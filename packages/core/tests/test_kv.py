"""Redis 键值层单测（ADR-0005 阶段三，全程零网络：fake client 注入，不引 fakeredis）。

覆盖：键形状与前缀 / 未配置退化为进程内 LRU（INFO 一次）/ 注入客户端后读写与 TTL /
      ping 失败粘性降级（WARNING 一次）/ 读写异常绝不上抛 / SET NX 租约语义 /
      脏缓存数据等同未命中 / 进程内 LRU 容量淘汰。
对齐：docs/ADR-0005 §3/§4；AGENTS.md §3（降级绝不 500）。
"""

from __future__ import annotations

import json
import logging

import pytest

from office_agent_core import kv
from office_agent_core.settings import settings


class FakeRedis:
    """最小 redis.asyncio 替身：只实现 kv 用到的 ping / get / set（含 NX 与 EX）。"""

    def __init__(self, *, ping_ok: bool = True, fail: tuple[str, ...] = ()) -> None:
        self.store: dict[str, str] = {}
        self.writes: list[tuple[str, int | None, bool]] = []
        self.ping_ok = ping_ok
        self.fail = set(fail)

    async def ping(self) -> bool:
        if not self.ping_ok:
            raise ConnectionError("ping down")
        return True

    async def get(self, key: str) -> str | None:
        if "get" in self.fail:
            raise ConnectionError("get down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if "set" in self.fail:
            raise ConnectionError("set down")
        self.writes.append((key, ex, nx))
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


@pytest.fixture(autouse=True)
def _clean_kv(monkeypatch: pytest.MonkeyPatch):
    """逐用例清零：kv 是模块级进程状态，且本机 .env 一登记 REDIS_URL 就会真连网络。"""
    monkeypatch.setattr(settings, "REDIS_URL", "")
    kv.reset()
    yield
    kv.reset()


# ---------------- ① 键形状 ----------------


def test_key_shape_and_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """向量键 = {prefix}:emb:{model}:{sha256(model+text)}；锁键 = {prefix}:{name}。"""
    monkeypatch.setattr(settings, "REDIS_KEY_PREFIX", "oa")
    key = kv.vector_key("qwen3-embedding:0.6b", "制度正文")
    assert key.startswith("oa:emb:qwen3-embedding:0.6b:")
    assert len(key.rsplit(":", 1)[1]) == 64  # sha256 hex
    assert key == kv.vector_key("qwen3-embedding:0.6b", "制度正文")  # 同输入稳定
    assert key != kv.vector_key("other-model", "制度正文")  # 换模型不串味
    assert key != kv.vector_key("qwen3-embedding:0.6b", "制度正文改")
    assert kv.lock_key("scheduler:tick") == "oa:scheduler:tick"
    # 前缀留空兜底 "oa"（绝产生以冒号开头的键）
    monkeypatch.setattr(settings, "REDIS_KEY_PREFIX", "  ")
    assert kv.lock_key("scheduler:tick") == "oa:scheduler:tick"


# ---------------- ② 未配置：进程内 LRU + 无锁单实例语义 ----------------


async def test_unconfigured_uses_memory_and_unlocked_semantics(caplog) -> None:
    """REDIS_URL 空 → 读写走进程内 LRU（同进程可见）、锁恒 True，且只记一次 INFO（非告警）。"""
    key = kv.vector_key("m", "文本")
    with caplog.at_level(logging.INFO, logger="office_agent_core.kv"):
        assert await kv.get_vector(key) is None  # 未命中
        await kv.set_vector(key, [1.5, 2.0])
        assert await kv.get_vector(key) == [1.5, 2.0]
        assert await kv.try_lock(kv.lock_key("scheduler:tick"), 10) is True
        assert await kv.try_lock(kv.lock_key("scheduler:tick"), 10) is True  # 无锁语义：不互斥
    notices = [r for r in caplog.records if r.name == "office_agent_core.kv"]
    assert len(notices) == 1, [r.getMessage() for r in notices]  # 只提示一次，不刷屏
    assert notices[0].levelno == logging.INFO  # 未配置是默认口径：INFO 不告警


# ---------------- ③ 注入客户端：真走 Redis，键/TTL 正确 ----------------


async def test_installed_client_is_used_with_ttl(caplog) -> None:
    fake = FakeRedis()
    kv.install_client(fake)
    key = kv.vector_key("m", "文本")
    await kv.set_vector(key, [1.0, 2.0])
    assert json.loads(fake.store[key]) == [1.0, 2.0]
    assert fake.writes == [(key, kv.VECTOR_CACHE_TTL_SECONDS, False)]  # SETEX 7 天
    assert await kv.get_vector(key) == [1.0, 2.0]
    assert not caplog.records  # 正常路径零告警


async def test_try_lock_set_nx_semantics(caplog) -> None:
    """锁：首个调用 SET NX 成功 True；他实例持有（键已存在）返回 False。"""
    fake = FakeRedis()
    kv.install_client(fake)
    key = kv.lock_key("scheduler:tick")
    assert await kv.try_lock(key, 60) is True
    assert fake.writes[-1] == (key, 60, True)  # NX + EX
    assert await kv.try_lock(key, 60) is False  # 键已被持有 → 本轮跳过


# ---------------- ④ 降级：连接失败/读写异常一律不抛 ----------------


async def test_ping_failure_degrades_sticky_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """配了 URL 但对端不可用 → 粘性降级（只探测一次）：读写退进程内、锁恒 True，WARNING 一次。"""
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(kv, "_new_client", lambda: FakeRedis(ping_ok=False))
    key = kv.vector_key("m", "文本")
    with caplog.at_level(logging.WARNING, logger="office_agent_core.kv"):
        assert await kv.get_vector(key) is None
        await kv.set_vector(key, [3.0])
        assert await kv.get_vector(key) == [3.0]  # 降级后仍可用（进程内）
        assert await kv.try_lock(key, 5) is True  # 无锁单实例语义
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


async def test_read_and_write_errors_never_raise(caplog) -> None:
    """后端读/写中途抛错：读按未命中、写静默放弃、锁按无锁语义，绝不 500。"""
    fake = FakeRedis(fail=("get", "set"))
    kv.install_client(fake)
    with caplog.at_level(logging.WARNING, logger="office_agent_core.kv"):
        assert await kv.get_vector(kv.vector_key("m", "文本")) is None
        await kv.set_vector(kv.vector_key("m", "文本"), [1.0])  # 不抛
        assert await kv.try_lock(kv.lock_key("scheduler:tick"), 5) is True
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_corrupt_payload_treated_as_miss() -> None:
    """存量脏数据（非 JSON / 非数值列表）等同未命中——重算后覆盖，不让缓存污染检索。"""
    fake = FakeRedis()
    kv.install_client(fake)
    key = kv.vector_key("m", "文本")
    fake.store[key] = "not-json"
    assert await kv.get_vector(key) is None
    fake.store[key] = json.dumps([1.0, "坏值"])
    assert await kv.get_vector(key) is None
    fake.store[key] = json.dumps([])
    assert await kv.get_vector(key) is None


# ---------------- ⑤ 进程内 LRU 容量淘汰 ----------------


async def test_memory_cache_evicts_oldest() -> None:
    """容量上限 MEMORY_CACHE_CAPACITY：超出淘汰最久未用（防长跑进程内存无界增长）。"""
    keys = [kv.vector_key("m", f"文本{i}") for i in range(kv.MEMORY_CACHE_CAPACITY + 1)]
    for index, key in enumerate(keys):
        await kv.set_vector(key, [float(index)])
    assert await kv.get_vector(keys[0]) is None  # 最久未用被淘汰
    assert await kv.get_vector(keys[-1]) == [float(kv.MEMORY_CACHE_CAPACITY)]
    assert await kv.get_vector(keys[1]) == [1.0]  # 其余保留
