"""可选 Redis 键值层（ADR-0005 阶段三：embedding 缓存 + 调度环多实例锁）。

职责：用三条窄接口（``get_vector`` / ``set_vector`` / ``try_lock``）收口进程外键值能力，
对调用方完全透明——未配置 ``REDIS_URL`` 或连接失败即退化为进程内语义，**绝不 500**：
- 缓存：进程内 OrderedDict LRU（容量 ``MEMORY_CACHE_CAPACITY``），语义同 Redis，只是仅本进程可见；
- 锁：无锁单实例语义（``try_lock`` 恒 True）——单实例部署本就无需互斥，绝不能因缓存后端
  缺席让调度环停摆。

明确不做（防过度工程，ADR-0005 §3）：LangGraph checkpointer 进 Redis、会话缓存、
任务队列 / Celery、业务数据缓存、redlock 库。

降级可观测口径：``REDIS_URL`` 未配置属默认口径（.env.example 已文档化），只记一次 INFO；
连接/读写失败属异常口径，记一次 WARNING——同一进程内不重复刷屏。

链路：retrieval.embed_texts（向量缓存）/ scheduler._tick_once（tick 租约锁）→ 本模块
      →（redis.asyncio，未配置或不可用即降级进程内）。
红线：只读写缓存与租约锁，不碰业务数据；凭据只走环境变量（本模块不打印 URL，防口令入日志）。
对齐：docs/ADR-0005 §3/§4；.trae/documents/LangGraph-Milvus-Redis三阶段迁移方案.md §阶段三。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from typing import Any

from office_agent_core.settings import settings

logger = logging.getLogger(__name__)

#: 进程内降级缓存的容量上限（LRU：超出淘汰最久未用，防长跑进程内存无界增长）
MEMORY_CACHE_CAPACITY = 2048
#: 向量缓存 TTL（7 天）：键中含模型名，换 embedding 模型时旧键自然过期不误用
VECTOR_CACHE_TTL_SECONDS = 7 * 24 * 3600

_initialized = False
_usable: Any = None
_notified = False
_memory: OrderedDict[str, list[float]] = OrderedDict()


def _prefix() -> str:
    """键前缀（配置为空时兜底 "oa"，绝不产生以冒号开头的键）。"""
    return settings.REDIS_KEY_PREFIX.strip() or "oa"


def vector_key(model: str, text: str) -> str:
    """向量缓存键：``{prefix}:emb:{model}:{sha256(model+text)}``（模型名进键，换模型不串味）。"""
    digest = hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()
    return f"{_prefix()}:emb:{model}:{digest}"


def lock_key(name: str) -> str:
    """租约锁键：``{prefix}:{name}``（name 自带层级，如 ``scheduler:tick``）。"""
    return f"{_prefix()}:{name}"


def _notify(reason: str, *, warning: bool) -> None:
    """降级提示（同一进程只报一次：要么未配置的 INFO，要么失败的 WARNING）。"""
    global _notified
    if _notified:
        return
    _notified = True
    log = logger.warning if warning else logger.info
    log("Redis 键值层退化为进程内语义：%s（ADR-0005 阶段三）", reason)


def _new_client() -> Any:
    """建 redis 客户端（可选依赖：缺失即抛 ImportError，由调用处降级裁决）。"""
    from redis.asyncio import Redis  # 可选依赖：未安装不影响检索与调度

    timeout = float(settings.REDIS_TIMEOUT_SECONDS)
    return Redis.from_url(
        settings.REDIS_URL.strip(),
        decode_responses=True,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
    )


async def _client_or_none() -> Any | None:
    """懒初始化 + 一次性 ping 探测（进程内只探测一次；失败粘性降级，不反复重连）。"""
    global _initialized, _usable
    if _initialized:
        return _usable
    _initialized = True
    if not settings.REDIS_URL.strip():
        _notify("REDIS_URL 未配置（缓存退化为进程内 LRU、锁为无锁单实例语义）", warning=False)
        return None
    try:
        client = _new_client()
        await client.ping()
    except Exception as exc:  # 依赖缺失 / 连接失败 / 认证失败一律降级
        _notify(f"连接不可用（{type(exc).__name__}: {str(exc)[:120]}）", warning=True)
        return None
    _usable = client
    return _usable


def _memory_get(key: str) -> list[float] | None:
    vector = _memory.get(key)
    if vector is None:
        return None
    _memory.move_to_end(key)  # LRU：命中即刷新新鲜度
    return vector


def _memory_set(key: str, vector: list[float]) -> None:
    _memory[key] = list(vector)
    _memory.move_to_end(key)
    while len(_memory) > MEMORY_CACHE_CAPACITY:
        _memory.popitem(last=False)


async def get_vector(key: str) -> list[float] | None:
    """取向量：未命中 / 后端不可用 / 存量数据非法一律返回 None（调用方按未命中重算），绝不抛。"""
    client = await _client_or_none()
    if client is None:
        return _memory_get(key)
    try:
        raw = await client.get(key)
    except Exception as exc:
        _notify(f"读取失败（{type(exc).__name__}），本次按未命中", warning=True)
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, list) or not payload:
            return None
        return [float(value) for value in payload]
    except (TypeError, ValueError):
        return None  # 脏数据等同未命中：重算后覆盖写入，不让缓存污染检索


async def set_vector(
    key: str, vector: list[float], ttl_seconds: int = VECTOR_CACHE_TTL_SECONDS
) -> None:
    """写向量：写失败只记一次告警并放弃本次缓存，绝不抛（缓存是加速器不是依赖）。"""
    client = await _client_or_none()
    if client is None:
        _memory_set(key, vector)
        return
    try:
        await client.set(key, json.dumps(vector), ex=int(ttl_seconds))
    except Exception as exc:
        _notify(f"写入失败（{type(exc).__name__}），本次放弃缓存", warning=True)


async def try_lock(key: str, ttl_seconds: float) -> bool:
    """抢租约锁（``SET key NX EX ttl``）：抢到 True，他实例持有则 False。

    后端不可用按**无锁单实例语义**返回 True——多实例护栏是增益，不是调度环的前置依赖；
    调度环因缓存后端缺席而停摆，比偶发重复触发更不可接受（降级绝不阻断红线）。
    """
    client = await _client_or_none()
    if client is None:
        return True
    try:
        acquired = await client.set(key, "1", nx=True, ex=max(1, int(ttl_seconds)))
    except Exception as exc:
        _notify(f"加锁失败（{type(exc).__name__}），按无锁单实例语义继续", warning=True)
        return True
    return bool(acquired)


def install_client(client: Any) -> None:
    """测试钩子：直接注入已就绪客户端（跳过建连与 ping 探测，全程零网络）。"""
    global _initialized, _usable
    _initialized = True
    _usable = client


def reset() -> None:
    """测试钩子：复位进程级状态（注入/探测标志与进程内缓存一并清空）。"""
    global _initialized, _usable, _notified
    _initialized = False
    _usable = None
    _notified = False
    _memory.clear()
