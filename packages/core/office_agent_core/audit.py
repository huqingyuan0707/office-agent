"""工具调用审计出口（内核只发事件，怎么落库由宿主决定）

链路：executor 的成功/失败两条路径都 await emit(record)
      → 默认只进进程内环形缓冲（开箱即用、测试可断言）
      → 宿主（server）启动时 set_sink(SqlAuditSink) 改写数据库 tool_calls 表。

为什么要这层缝：内核不 import 任何 ORM，才能被独立安装与单测；
审计是「好/坏两条路径都要写」的硬要求，所以出口必须存在且默认不为空。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

_MAX_RECENT = 200


@dataclass(frozen=True)
class AuditRecord:
    """一次工具调用的审计记录（成功与失败同结构，用 ok 区分）。"""

    trace_id: str
    tenant: str
    username: str
    name: str
    args: dict[str, Any]
    result: dict[str, Any]
    latency_ms: int
    ok: bool
    #: 宿主注入的存储句柄（内核不解释其类型，只转交给 sink）
    db: Any = None
    #: 远程工具的实际发起人（透传给上游做审计留痕；本地工具与 username 相同）
    on_behalf_of: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """可序列化投影（不含 db 句柄）。"""
        return {
            "trace_id": self.trace_id,
            "tenant": self.tenant,
            "username": self.username,
            "name": self.name,
            "args": self.args,
            "result": self.result,
            "latency_ms": self.latency_ms,
            "ok": self.ok,
            "on_behalf_of": self.on_behalf_of,
        }


AuditSink = Callable[[AuditRecord], Awaitable[None]]

_lock = threading.Lock()
_recent: deque[AuditRecord] = deque(maxlen=_MAX_RECENT)
_sink: AuditSink | None = None


def set_sink(sink: AuditSink | None) -> None:
    """注册落库实现（宿主导入 server 后调用；传 None 回到内存态）。"""
    global _sink
    with _lock:
        _sink = sink


async def emit(record: AuditRecord) -> None:
    """记录一条审计：进环形缓冲并投递给 sink；任何失败都不阻断主流程。"""
    with _lock:
        _recent.append(record)
        sink = _sink
    if sink is None:
        return
    try:
        await sink(record)
    except Exception:
        return


def recent(limit: int = 20) -> list[dict[str, Any]]:
    """最近若干条审计（进程内视图，巡检与测试用）。"""
    with _lock:
        items = list(_recent)[-max(1, limit) :]
    return [item.to_dict() for item in items]


def count() -> int:
    """环形缓冲当前条数（测试断言用，不代表历史总量）。"""
    with _lock:
        return len(_recent)


def reset() -> None:
    """清空环形缓冲（仅测试用；不动 sink）。"""
    with _lock:
        _recent.clear()
