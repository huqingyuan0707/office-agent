"""行政后勤通用工具（office.desk.ticket / tickets，对齐 PRD §2.11「通用工具」）。

职责：
- office.desk.ticket（office:write + 恒送审 + 幂等键必带）：IT 报修 / 资产申领 /
  工单创建——kind + 标题 + 详情落盘 DOCS_DIR/data/desk_tickets.json（审批通过
  后才落单，与 todo.create 同链）；
- office.desk.tickets（office:read）：本人租户的工单清单（kind/status 过滤），
  带 source + fetched_at 溯源。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler；
      台账读写锁 + asyncio.to_thread（affairs 同一磁盘纪律）。
红线：kind 白名单三类（it_repair/asset_claim/work_order），自由文本拒绝；
      状态机只认 open/done，流转走 office.todo.update 同口径（本模块不另起状态）；
      损坏台账 1001 中文，绝不 500。
如实后置：工单的外部项目管理系统同步（§2.9 commit 同口径）不在本模块——落库即
      本地台账，同步由 linkage 白名单工具承担。
对齐：AGENTS.md §3（写动作恒送审/降级不 500/溯源）；智能办公Agent 产品需求文档.md §2.11。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"
SCOPE_WRITE = "office:write"

_KINDS = {"it_repair": "IT 报修", "asset_claim": "资产申领", "work_order": "工单创建"}
_KIND_LIST = "it_repair/asset_claim/work_order"

_STORE_LOCK = asyncio.Lock()


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _store_path() -> Path:
    """工单台账路径（DOCS_DIR/data/desk_tickets.json；惰性建目录）。"""
    root = Path(settings.DOCS_DIR).resolve() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root / "desk_tickets.json"


def _read_tickets() -> list[dict[str, Any]]:
    """读台账：缺文件即空；损坏转 1001 中文（绝不 500）。"""
    path = _store_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(ErrorCode.PARAM_INVALID, "工单台账损坏（无法解析）") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tickets"), list):
        raise BusinessError(ErrorCode.PARAM_INVALID, "工单台账损坏（根节点缺 tickets 数组）")
    return [t for t in payload["tickets"] if isinstance(t, dict)]


async def _desk_ticket(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.desk.ticket：行政后勤工单落单（审批通过后才被 decide 触发）。"""
    kind = str(args.get("kind") or "").strip()
    if kind not in _KINDS:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 kind 只能是 {_KIND_LIST}（当前：{kind}）"
        )
    title = str(args.get("title") or "").strip()
    if not title or len(title) > 200:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 title 必须为 1-200 字的非空文本")
    detail = str(args.get("detail") or "").strip()[:2000]
    idem_key = str(args.get("idem_key") or "").strip()
    record = {
        "id": f"desk-{uuid.uuid4().hex[:8]}",
        "tenant": ctx.tenant,
        "kind": kind,
        "kind_label": _KINDS[kind],
        "title": title,
        "detail": detail,
        "status": "open",
        "created_by": ctx.username,
        "created_at": _now_text(),
        "idem_key": idem_key,
    }

    def _write() -> None:
        tickets = _read_tickets()
        tickets.append(record)
        _store_path().write_text(
            json.dumps({"tickets": tickets}, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async with _STORE_LOCK:
        await asyncio.to_thread(_write)
    return {**record, "note": "工单已落本地台账；流转（完成）请走 office.todo.update 同口径"}


async def _desk_tickets(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.desk.tickets：本人租户的工单清单（kind/status 过滤，读免审）。"""
    kind = str(args.get("kind") or "").strip()
    if kind and kind not in _KINDS:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 kind 只能是 {_KIND_LIST}（当前：{kind}）"
        )
    status = str(args.get("status") or "").strip()
    if status and status not in ("open", "done"):
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 status 只能是 open/done（当前：{status}）"
        )

    def _load() -> list[dict[str, Any]]:
        return [
            t
            for t in _read_tickets()
            if t.get("tenant") == ctx.tenant
            and (not kind or t.get("kind") == kind)
            and (not status or t.get("status") == status)
        ]

    async with _STORE_LOCK:
        items = await asyncio.to_thread(_load)
    return {
        "tickets": items,
        "count": len(items),
        "source": "local-desk-tickets",
        "fetched_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """后勤两工具的 ToolSpec（ticket 写恒送审 + 幂等键；tickets 读免审）。"""
    return (
        ToolSpec(
            name="office.desk.ticket",
            scope=SCOPE_WRITE,
            description="行政后勤工单创建（写动作）：IT 报修/资产申领/工单创建三类，恒送审 + "
            "idem_key 必填，审批通过后落本地台账",
            params={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "工单类型（it_repair/asset_claim/work_order）",
                        "enum": ["it_repair", "asset_claim", "work_order"],
                    },
                    "title": {
                        "type": "string",
                        "description": "工单标题（1-200 字）",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "detail": {"type": "string", "description": "详情说明", "maxLength": 2000},
                    "idem_key": {
                        "type": "string",
                        "description": "幂等键（8-64 字符）",
                        "minLength": 8,
                        "maxLength": 64,
                    },
                },
                "required": ["kind", "title", "idem_key"],
                "additionalProperties": False,
            },
            handler=_desk_ticket,
            idempotent=True,
            requires_approval=True,
            approval_action="office.desk.ticket",
        ),
        ToolSpec(
            name="office.desk.tickets",
            scope=SCOPE_READ,
            description="行政后勤工单清单：本人租户的 kind/status 过滤列表，带溯源",
            params={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "工单类型过滤",
                        "enum": ["it_repair", "asset_claim", "work_order"],
                    },
                    "status": {
                        "type": "string",
                        "description": "状态过滤（open/done）",
                        "enum": ["open", "done"],
                    },
                },
                "additionalProperties": False,
            },
            handler=_desk_tickets,
        ),
    )


def register_all() -> list[str]:
    """注册后勤两工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
