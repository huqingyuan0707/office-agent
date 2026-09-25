"""常用审批单一键提交（office.approval.submit，对齐 PRD §2.3「补齐后才成稿提交」）。

职责：
- office.approval.submit（office:write + 恒送审 + idem_key 必填）：draft/check 齐备后
  正式提交请假/报销/采购/加班/出差五类审批单——invoke 即落审批单（pending），
  复核员批准后 handler 才把单据写入本地台账（DOCS_DIR/data/approvals_ledger.json，
  读写锁仿 affairs）；台账即「单据生效」的演示落点（生产应推 HR/财务系统，数据不出域）。
- 幂等回放：(tenant, idem_key) 已落账 → 返回 replayed=True 与原记录，绝不双单
  （与联动回流 ticket.create 同一口径）。
- 执行期复核：必填缺失的单据批准时诚实 1001 拒收（不落成残缺台账）。

链路：executor 送审落单 → api/approvals.approve → decide_approval 以申请人身份执行
      本 handler → 台账落盘。模板口径 import approval.KIND_SPECS/missing_fields（唯一出处）。
红线：写动作恒送审（免审直生单的路径不存在）；idem_key 必填；出参带 source + fetched_at；
      纯本地实现，不触及 ORM / FastAPI。
对齐：AGENTS.md §3（写动作恒送审/幂等键/溯源）；智能办公Agent 产品需求文档.md
      §2.3（一键发起——必填缺失追问由 office.approval.draft 承担，本工具是「补齐后提交」）。
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
from office_agent_tools_office.approval import KIND_SPECS, kind_or_raise, missing_fields

logger = logging.getLogger(__name__)

SCOPE_WRITE = "office:write"

#: 台账存储（演示落点；生产替换为对接 HR/财务的白名单工具，数据不出域红线不变）
_LEDGER_LOCK = asyncio.Lock()


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ledger_path() -> Path:
    root = Path(settings.DOCS_DIR).resolve() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root / "approvals_ledger.json"


def _read_ledger() -> list[dict[str, Any]]:
    path = _ledger_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"审批台账损坏（非合法 JSON）：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tickets"), list):
        raise BusinessError(ErrorCode.PARAM_INVALID, "审批台账损坏（根节点缺 tickets 列表）")
    return payload["tickets"]


def _write_ledger(tickets: list[dict[str, Any]]) -> None:
    _ledger_path().write_text(
        json.dumps({"tickets": tickets}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _append_ticket(record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """按 (tenant, idem_key) 幂等追加：已存在返回 (原记录, True)，否则落盘返回 (新记录, False)。"""
    tickets = _read_ledger()
    for ticket in tickets:
        if (
            ticket.get("tenant") == record["tenant"]
            and ticket.get("idem_key") == record["idem_key"]
        ):
            return ticket, True
    tickets.append(record)
    _write_ledger(tickets)
    return record, False


async def _approval_submit(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.approval.submit：提交常用审批单（本 handler 只在复核员批准后执行）。"""
    kind = kind_or_raise(args.get("kind"))
    fields = args.get("fields") or {}
    if not isinstance(fields, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 fields 必须是键值对对象")
    idem_key = str(args.get("idem_key") or "").strip()
    if len(idem_key) < 8:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 idem_key 至少 8 字符（幂等回放依据）")
    filled = {key: str(value).strip() for key, value in fields.items() if str(value).strip()}
    missing = missing_fields(kind, filled)
    if missing:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"必填项缺失不能成稿：{'、'.join(missing)}（先用 office.approval.draft 补齐再提交）",
        )
    record = {
        "ticket_id": uuid.uuid4().hex[:12],
        "tenant": ctx.tenant,
        "applicant": ctx.username,
        "kind": kind,
        "kind_label": KIND_SPECS[kind]["label"],
        "fields": filled,
        "idem_key": idem_key,
        "status": "approved",  # 能执行到这里说明复核闸门已通过，单据即生效
        "submitted_at": _now_text(),
    }
    async with _LEDGER_LOCK:
        ticket, replayed = await asyncio.to_thread(_append_ticket, record)
    if not replayed:
        logger.info(
            "审批单落账：ticket=%s kind=%s applicant=%s", ticket["ticket_id"], kind, ctx.username
        )
    return {
        "ticket": ticket,
        "replayed": replayed,
        "count_note": (
            "同 idem_key 已落账：幂等回放返回原单，绝不双单"
            if replayed
            else "单据已生效（复核批准后落台账）"
        ),
        "source": "local-ledger:approvals_ledger.json",
        "fetched_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """office.approval.submit 的 ToolSpec（写口径，恒送审 + idem_key 必填）。"""
    return (
        ToolSpec(
            name="office.approval.submit",
            scope=SCOPE_WRITE,
            requires_approval=True,
            description="正式提交常用审批单（请假/报销/采购/加班/出差）：写动作恒送审 + idem_key 必填，复核批准后才落审批台账生效；必填缺失执行期诚实拒收；同键重放幂等返回原单绝不双单",
            params={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "单据类型（leave/expense/purchase/overtime/trip）",
                        "enum": sorted(KIND_SPECS),
                    },
                    "fields": {
                        "type": "object",
                        "description": "单据字段（必填口径见 office.approval.draft 模板）",
                    },
                    "idem_key": {
                        "type": "string",
                        "description": "幂等键（至少 8 字符；重放返回原单不双落账）",
                        "minLength": 8,
                    },
                },
                "required": ["kind", "fields", "idem_key"],
                "additionalProperties": False,
            },
            handler=_approval_submit,
        ),
    )


def register_all() -> list[str]:
    """注册审批单提交工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
