"""审计落库实现（内核 audit sink 的宿主侧注入点）

链路：executor 好坏两条路径都 await audit.emit(AuditRecord)
      → app 工厂启动时 set_sink(sql_audit_sink) → 本模块写 tool_calls 表。

为什么放在壳里：内核（office_agent_core）不依赖任何 ORM，才能独立安装与单测；
审计记录里的 ``result`` 含远程工具出参，因此远程调用数据里的 ``provenance``
（数据出处与取数时刻）在审计表里天然可查——这是「数值一致率」追溯的依据。
"""

from __future__ import annotations

import json
from typing import Any

from office_agent_core.audit import AuditRecord
from office_agent_server.models import ToolCall


def _dumps(value: Any) -> str:
    """JSON 文本落库（非可序列化对象降级为 str，绝不因落审计失败丢记录）。"""
    return json.dumps(value, ensure_ascii=False, default=str)


async def sql_audit_sink(record: AuditRecord) -> None:
    """把一条审计写进 tool_calls（会话由内核经 AuditRecord.db 转交；无会话则跳过）。"""
    session = record.db
    if session is None:
        return
    session.add(
        ToolCall(
            trace_id=record.trace_id,
            tenant=record.tenant,
            username=record.username,
            name=record.name,
            args=_dumps(record.args),
            result=_dumps(record.result),
            latency_ms=int(record.latency_ms),
        )
    )
    await session.flush()
