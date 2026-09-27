"""任务控制台展示口径与审计辅助（序列化/来源状态标签/结果入口/审计留痕）。

职责：把 Task 行转成前端视图 dict；agent.run 旧数据（无 name/source/ref_* 列）
按 checkpoint/input 推断展示名与来源（绝不编造缺失状态）；任务操作写
tool_calls 审计表（只追加不改）。
对齐：AGENTS.md §3（审计留痕/数据不出域）；产品口径「任务执行控制台」。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_server.models import Task, ToolCall

#: 任务来源取值白名单（扩展来源只在此登记）
SOURCE_CHOICES = {
    "chat": "对话办理",
    "docs": "文档中心",
    "breakdown": "任务拆解",
    "scheduled": "定时任务",
    "agent": "智能体",
    "manual": "手动登记",
}

#: 状态中文口径（modal 层展示用）
STATUS_LABELS = {
    "pending": "排队中",
    "running": "执行中",
    "waiting_approval": "待确认",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}

#: 结果入口探测字段（markdown 文档产物 / 链接 / 审批单，命中即给跳转入口）
_RESULT_KEYS = ("document", "report", "worklog", "minutes", "agenda", "markdown", "content")
_LINK_KEYS = ("url", "link", "file", "download_url")

_TASK_NAME_MAX = 80


def parse_json_text(raw: str, fallback: Any = None) -> Any:
    """JSON 文本 → 对象（脏数据给 fallback，绝不因历史脏列拖垮控制台）。"""
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _task_name(row: Task, checkpoint: dict[str, Any]) -> str:
    """任务名：name 列优先；agent.run 按 checkpoint 的 agent+goal 推断。"""
    if row.name:
        return row.name
    if row.type == "agent.run":
        agent = str(checkpoint.get("agent") or "")
        goal = str(checkpoint.get("goal") or "")
        if agent and goal:
            return f"{agent}：{goal}"[:_TASK_NAME_MAX]
        if goal:
            return goal[:_TASK_NAME_MAX]
    return str(row.type or "未命名任务")


def _task_source(row: Task) -> str:
    """来源：source 列优先，agent.run 老数据归 agent，绝不编造未知来源。"""
    if row.source:
        return row.source
    if row.type == "agent.run":
        return "agent"
    return "manual"


def _ref_of(row: Task, output: dict[str, Any]) -> tuple[str, str, str]:
    """关联对象三元组：列优先；agent.run 按输出里的 approval_id 推断。"""
    if row.ref_kind:
        return row.ref_kind, row.ref_id, row.ref_label
    approval_id = str(
        output.get("approval_id") or (output.get("pending_approval") or {}).get("approval_id") or ""
    )
    if approval_id:
        return "approval", approval_id, "待审批单"
    return "", "", ""


def _result_links(row: Task, output: dict[str, Any]) -> list[dict[str, str]]:
    """结果入口：输出里的审批单/文档产物/链接命中即给跳转提示（缺省空，绝不硬拼）。"""
    links: list[dict[str, str]] = []
    approval_id = str((output.get("pending_approval") or {}).get("approval_id") or "")
    if not approval_id:
        approval_id = str(output.get("approval_id") or "")
    if approval_id:
        links.append(
            {"kind": "approval", "label": "前往审批", "href": f"/approvals?id={approval_id}"}
        )
    for key in _RESULT_KEYS:
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            links.append({"kind": "doc", "label": "查看结果", "href": ""})
            break
    for key in _LINK_KEYS:
        value = output.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            links.append({"kind": "link", "label": "打开链接", "href": value})
            break
    return links


def serialize_task(row: Task, *, detail: bool = False) -> dict[str, Any]:
    """单任务视图：列表与详情共用，detail 时补 input/output/error/checkpoint 原文。"""
    checkpoint = parse_json_text(row.checkpoint, {})
    output = parse_json_text(row.output, {}) or {}
    error = parse_json_text(row.error, {}) or {}
    ref_kind, ref_id, ref_label = _ref_of(row, output)
    source = _task_source(row)
    entry: dict[str, Any] = {
        "id": row.id,
        "name": _task_name(row, checkpoint),
        "type": row.type,
        "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "source": source,
        "source_label": SOURCE_CHOICES.get(source, source),
        "progress": float(row.progress or 0),
        "username": row.username,
        "ref_kind": ref_kind,
        "ref_id": ref_id,
        "ref_label": ref_label,
        "result_links": _result_links(row, output),
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
        "updated_at": row.updated_at.isoformat(sep=" ", timespec="seconds"),
    }
    if detail:
        entry["input"] = checkpoint if isinstance(checkpoint, dict) else {}
        entry["output"] = output
        entry["error"] = error
        entry["checkpoint"] = checkpoint
        err_msg = error.get("message") or error.get("error")
        entry["error_hint"] = str(err_msg or "")[:300]
    return entry


async def audit_task_action(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    trace_id: str,
    action: str,
    task_id: str,
    detail: dict[str, Any],
) -> None:
    """任务操作留痕（tool_calls 审计表，只追加不改）。"""
    db.add(
        ToolCall(
            trace_id=trace_id or f"task:{task_id}",
            tenant=tenant,
            username=username,
            name=action,
            args=json.dumps({"task_id": task_id, **detail}, ensure_ascii=False, default=str),
            result="{}",
            latency_ms=0,
        )
    )
    await db.flush()
