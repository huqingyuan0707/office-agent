"""个人事务·待办域工具与共享存储原语（PRD §2.2：待办管理）。

职责：
- 本地事务存储（DOCS_DIR/data/affairs.json）：待办 + 日程两类记录，
  读写锁 + asyncio.to_thread（与 templates/pptx 同一套磁盘纪律）——
  存储原语（business_now/load_affairs/add_*/update_*/delete_*/_parse_*）唯一出处，
  日程域 affairs_schedule.py 只 import 不复制；
- office.todo.list（读）：本人待办按状态过滤清单；
- office.todo.update / office.todo.delete（写，恒送审）：修改（含标记完成）与删除。

链路：__init__.register_all() → registry → executor；server/services/notifications.py
经 load_affairs() 读同一存储做「任务到期/会议临近/项目节点」预警（import 方向 server→tools-office 既有合法）。
红线：写动作恒送审（审批通过才落盘）；时间解析失败 1001 可操作报错，绝不臆造；
出参带 source + fetched_at 溯源；跨人记录按 tenant+owner 双过滤，越权不可见。
对齐：AGENTS.md §3（写动作恒送审/降级不 500/溯源）；智能办公Agent 产品需求文档.md §2.2。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"
SCOPE_WRITE = "office:write"

#: 工作时段（空闲时间查询的口径，台账与提醒共用同一日界）
_WORK_START_MIN = 9 * 60
_WORK_END_MIN = 18 * 60

_STORE_LOCK = asyncio.Lock()


def business_now() -> datetime:
    """业务时区的当前时间（到期/临近判定唯一时基，Settings.BUSINESS_TIMEZONE 可配）。

    Windows 无系统 tz 库时 ZoneInfo 依赖 tzdata 包；缺失即降级 UTC（只告警不抛错，
    绝不因时区数据缺失炸掉整条扫描链/工具执行——降级绝不 500）。
    """
    try:
        return datetime.now(ZoneInfo(settings.BUSINESS_TIMEZONE))
    except Exception:  # ZoneInfoNotFoundError / 时区键非法
        logger.warning("业务时区 %s 不可用（缺 tzdata？），降级 UTC", settings.BUSINESS_TIMEZONE)
        return datetime.now(UTC)


def _now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _store_path() -> Path:
    root = Path(settings.DOCS_DIR).resolve() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root / "affairs.json"


def _read_store() -> dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return {"todos": [], "schedules": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"事务存储损坏（非合法 JSON）：{exc}") from exc
    if not isinstance(payload, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, "事务存储损坏（根节点非对象）")
    payload.setdefault("todos", [])
    payload.setdefault("schedules", [])
    return payload


def _write_store(payload: dict[str, Any]) -> None:
    _store_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def load_affairs(tenant: str) -> dict[str, Any]:
    """按租户读取事务存储（server 通知扫描共用入口）；缺文件视为空库。"""

    def _load() -> dict[str, Any]:
        payload = _read_store()
        return {
            "todos": [t for t in payload["todos"] if t.get("tenant") == tenant],
            "schedules": [s for s in payload["schedules"] if s.get("tenant") == tenant],
        }

    return await asyncio.to_thread(_load)


def _parse_date(value: Any, field: str) -> date:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 {field} 需为 YYYY-MM-DD 日期（当前：{text or '空'}）"
        ) from exc


def _parse_dt(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"参数 {field} 需为 YYYY-MM-DD HH:MM 本地时间（当前：{text or '空'}）",
        ) from exc


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------- 存储写入原语（审批通过后的执行路径调用） ----------------


async def add_todo(
    tenant: str,
    owner: str,
    *,
    title: str,
    description: str = "",
    priority: str = "medium",
    due_date: str = "",
) -> dict[str, Any]:
    """落一条待办（todo.create 执行体；due_date 空 = 不设截止）。"""
    if due_date:
        _parse_date(due_date, "due_date")
    record = {
        "id": _new_id(),
        "tenant": tenant,
        "owner": owner,
        "title": title[:200],
        "description": description[:1000],
        "priority": priority,
        "due_date": due_date,
        "status": "open",
        "created_at": _now_text(),
        "updated_at": _now_text(),
        "completed_at": "",
    }

    async with _STORE_LOCK:
        payload = await asyncio.to_thread(_read_store)
        payload["todos"].append(record)
        await asyncio.to_thread(_write_store, payload)
    return record


async def update_todo(
    tenant: str, owner: str, todo_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    """按 id 修改本人待办（status=done 时记 completed_at，作为台账「已完成」口径）。"""
    async with _STORE_LOCK:
        payload = await asyncio.to_thread(_read_store)
        item = _find_todo_sync(payload, tenant, owner, todo_id)
        allowed = {"title", "description", "priority", "due_date", "status"}
        for key, value in patch.items():
            if key not in allowed:
                raise BusinessError(ErrorCode.PARAM_INVALID, f"不支持修改的字段：{key}")
            if key == "due_date" and value:
                _parse_date(value, "due_date")
            if key == "status" and value not in ("open", "done", "cancelled"):
                raise BusinessError(
                    ErrorCode.PARAM_INVALID, "status 只能是 open / done / cancelled"
                )
            item[key] = value
        if patch.get("status") == "done":
            item["completed_at"] = _now_text()
        if patch.get("status") == "open":
            item["completed_at"] = ""
        item["updated_at"] = _now_text()
        await asyncio.to_thread(_write_store, payload)
    return item


def _find_todo_sync(payload: dict[str, Any], tenant: str, owner: str, todo_id: str) -> dict:
    """锁内同步查找（update/delete 共用；找不到抛 404）。"""
    for item in payload["todos"]:
        if (
            item.get("id") == todo_id
            and item.get("tenant") == tenant
            and item.get("owner") == owner
        ):
            return item
    raise BusinessError(
        ErrorCode.NOT_FOUND,
        f"待办不存在或不属于你：{todo_id}（可用 office.todo.list 查看本人待办 id）",
        404,
    )


async def delete_todo(tenant: str, owner: str, todo_id: str) -> dict[str, Any]:
    """按 id 删除本人待办（回执带被删标题，删除幂等二次调用返回 404）。"""
    async with _STORE_LOCK:
        payload = await asyncio.to_thread(_read_store)
        item = _find_todo_sync(payload, tenant, owner, todo_id)
        payload["todos"] = [t for t in payload["todos"] if t.get("id") != todo_id]
        await asyncio.to_thread(_write_store, payload)
    return {"deleted": todo_id, "title": item.get("title", "")}


# ---------------- 工具 handler ----------------


async def _todo_list(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.todo.list：本人待办清单（status=open/done/cancelled/all，缺省 open）。"""
    status = str(args.get("status") or "open").strip()
    if status not in ("open", "done", "cancelled", "all"):
        raise BusinessError(ErrorCode.PARAM_INVALID, "status 只能是 open / done / cancelled / all")
    data = await load_affairs(ctx.tenant)
    items = [t for t in data["todos"] if t.get("owner") == ctx.username]
    if status != "all":
        items = [t for t in items if t.get("status") == status]
    items.sort(key=lambda t: (t.get("due_date") or "9999-12-31", t.get("id", "")))
    return {
        "status_filter": status,
        "count": len(items),
        "items": items,
        "source": f"local-affairs-store:{ctx.username}",
        "fetched_at": _now_text(),
    }


async def _todo_update(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.todo.update：修改本人待办（审批通过后执行落盘）。"""
    todo_id = str(args.get("todo_id") or "").strip()
    if not todo_id:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, "参数 todo_id 不能为空（office.todo.list 可查）"
        )
    patch = {
        key: args[key]
        for key in ("title", "description", "priority", "due_date", "status")
        if key in args
    }
    if not patch:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            "至少要修改一个字段（title/description/priority/due_date/status）",
        )
    item = await update_todo(ctx.tenant, ctx.username, todo_id, patch)
    return {"updated": item, "note": "待办已更新（改动已落本地事务存储）"}


async def _todo_delete(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.todo.delete：删除本人待办（审批通过后执行）。"""
    todo_id = str(args.get("todo_id") or "").strip()
    if not todo_id:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, "参数 todo_id 不能为空（office.todo.list 可查）"
        )
    receipt = await delete_todo(ctx.tenant, ctx.username, todo_id)
    return {**receipt, "note": "待办已删除；如需留痕可先标记 status=cancelled"}


# ---------------- ToolSpec ----------------

_ID_DESC = "待办 id（office.todo.list 返回的 id 字段）"


def specs() -> tuple[ToolSpec, ...]:
    """待办域三工具的 ToolSpec（list 读免审；update/delete 写恒送审，审批通过才落盘）。"""
    return (
        ToolSpec(
            name="office.todo.list",
            scope=SCOPE_READ,
            description="查询本人待办清单：按状态过滤（open/done/cancelled/all），返回 id/标题/截止/优先级，带溯源",
            params={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "状态过滤（缺省 open）",
                        "enum": ["open", "done", "cancelled", "all"],
                    }
                },
                "additionalProperties": False,
            },
            handler=_todo_list,
        ),
        ToolSpec(
            name="office.todo.update",
            scope=SCOPE_WRITE,
            description="修改本人待办（写动作，恒送审）：可改标题/详情/优先级/截止日期，status=done 即标记完成（记完成时间，进台账口径）",
            params={
                "type": "object",
                "properties": {
                    "todo_id": {
                        "type": "string",
                        "description": _ID_DESC,
                        "minLength": 1,
                        "maxLength": 64,
                    },
                    "title": {"type": "string", "maxLength": 200},
                    "description": {"type": "string", "maxLength": 1000},
                    "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                    "due_date": {
                        "type": "string",
                        "description": "截止日期 YYYY-MM-DD",
                        "minLength": 10,
                        "maxLength": 10,
                    },
                    "status": {"type": "string", "enum": ["open", "done", "cancelled"]},
                },
                "required": ["todo_id"],
                "additionalProperties": False,
            },
            handler=_todo_update,
            idempotent=True,
            requires_approval=True,
            approval_action="office.todo.update",
        ),
        ToolSpec(
            name="office.todo.delete",
            scope=SCOPE_WRITE,
            description="删除本人待办（写动作，恒送审）：审批通过后从本地事务存储移除；只想留痕可改用 todo.update 标记 cancelled",
            params={
                "type": "object",
                "properties": {
                    "todo_id": {
                        "type": "string",
                        "description": _ID_DESC,
                        "minLength": 1,
                        "maxLength": 64,
                    }
                },
                "required": ["todo_id"],
                "additionalProperties": False,
            },
            handler=_todo_delete,
            idempotent=True,
            requires_approval=True,
            approval_action="office.todo.delete",
        ),
    )


def register_all() -> list[str]:
    """注册待办域工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
