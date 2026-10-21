"""批量建单的存储原语（幂等台账 + 一批任务一次性落盘，PRD §2.9 / §6.5）。

职责：
- commit_todos()：同 (tenant, idem_key) 已建过 → 回放原回执**绝不双单**；
  否则在锁内逐条落待办并登记台账（崩溃后重放同键也不重复建单）。

为什么单独成模块：affairs.py 是「待办域工具 + 存储原语」，批量建单只是一条写入路径，
拆出后 affairs.py 保持体量红线内；存储原语（读写锁/记录构造）仍唯一出处在 affairs.py，
本模块只 import 不复制（与 affairs_schedule.py 同一约定）。
红线：owner 必填（不臆造接收人——缺责任人在工具层先拒收）；同键回放不双单。
对齐：AGENTS.md §3（分层红线）；智能办公Agent 产品需求文档.md §6.5。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .affairs import (
    _STORE_LOCK,
    _now_text,
    _read_store,
    _todo_record,
    _write_store,
)


async def commit_todos(tenant: str, idem_key: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """批量建单（幂等口径）：同 (tenant, idem_key) 已建过 → 回放原回执**绝不双单**；
    否则在锁内逐条落待办并登记台账（崩溃后重放同键也不重复建单）。

    items 每项：``{title, owner, description, priority, due_date}``；owner 必填
    （不臆造接收人——缺责任人在工具层先拒收）。
    """
    async with _STORE_LOCK:
        payload = await asyncio.to_thread(_read_store)
        commits = payload.setdefault("commits", [])
        for record in commits:
            if record.get("tenant") == tenant and record.get("idem_key") == idem_key:
                return {**record, "replayed": True}
        created: list[dict[str, Any]] = []
        for item in items:
            record = _todo_record(
                tenant,
                str(item.get("owner") or ""),
                title=str(item.get("title") or ""),
                description=str(item.get("description") or ""),
                priority=str(item.get("priority") or "medium"),
                due_date=str(item.get("due_date") or ""),
            )
            payload["todos"].append(record)
            created.append(record)
        receipt = {
            "tenant": tenant,
            "idem_key": idem_key,
            "created": created,
            "created_at": _now_text(),
        }
        commits.append(receipt)
        await asyncio.to_thread(_write_store, payload)
    return {**receipt, "replayed": False}
