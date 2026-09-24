"""个人事务·日程域工具（PRD §2.2：日程会议 / 空闲查询 / 工作台账）。

职责（与 affairs.py 共用同一本地事务存储 DOCS_DIR/data/affairs.json）：
- office.schedule.create（写，恒送审）：一键创建日程（meeting 会议 / milestone 项目节点），
  参会人名单落库，会前提醒由 server 通知扫描链对本人+参会人逐人生成（站内通知，不假发外部邮件）；
- office.schedule.freebusy（读）：人员空闲时间查询——工作时段减去当日忙碌区间，实测计算；
- office.worklog.generate（读）：个人工作台账——日/周窗口内已完成、待完成与日程聚合直出。

链路：__init__.register_all() → registry → executor；存储原语（读写锁/business_now/
load_affairs/_parse_*）唯一出处在 affairs.py，本模块只 import 不复制。
红线：写动作恒送审（审批通过才落盘）；时间解析失败 1001 可操作报错，绝不臆造；
出参带 source + fetched_at 溯源。
对齐：AGENTS.md §3（写动作恒送审/溯源）；智能办公Agent 产品需求文档.md §2.2。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from .affairs import (
    _STORE_LOCK,
    _WORK_END_MIN,
    _WORK_START_MIN,
    SCOPE_READ,
    SCOPE_WRITE,
    _new_id,
    _now_text,
    _parse_date,
    _parse_dt,
    _read_store,
    _write_store,
    business_now,
    load_affairs,
)


async def add_schedule(
    tenant: str,
    owner: str,
    *,
    title: str,
    kind: str,
    start: str,
    duration_minutes: int,
    attendees: list[str],
    location: str = "",
) -> dict[str, Any]:
    """落一条日程（schedule.create 执行体）；start 为业务时区本地时间。"""
    if kind not in ("meeting", "milestone"):
        raise BusinessError(
            ErrorCode.PARAM_INVALID, "kind 只能是 meeting（会议）/ milestone（项目节点）"
        )
    start_dt = _parse_dt(start, "start")
    minutes = int(duration_minutes)
    if kind == "milestone":
        minutes = 0
    if minutes < 0 or minutes > 8 * 60:
        raise BusinessError(ErrorCode.PARAM_INVALID, "duration_minutes 需在 0-480 之间")
    record = {
        "id": _new_id(),
        "tenant": tenant,
        "owner": owner,
        "title": title[:200],
        "kind": kind,
        "start": start_dt.strftime("%Y-%m-%d %H:%M"),
        "end": ""
        if kind == "milestone"
        else (start_dt + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M"),
        "attendees": [str(a).strip()[:64] for a in attendees if str(a).strip()][:50],
        "location": location[:200],
        "created_at": _now_text(),
    }

    async with _STORE_LOCK:
        payload = await asyncio.to_thread(_read_store)
        payload["schedules"].append(record)
        await asyncio.to_thread(_write_store, payload)
    return record


# ---------------- 工具 handler ----------------


async def _schedule_create(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.schedule.create：一键创建日程（会议/项目节点，审批通过后落盘）。"""
    title = str(args.get("title") or "").strip()
    if not title:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 title 不能为空（日程主题）")
    attendees_raw = args.get("attendees")
    attendees = [str(a) for a in attendees_raw] if isinstance(attendees_raw, list) else []
    record = await add_schedule(
        ctx.tenant,
        ctx.username,
        title=title,
        kind=str(args.get("kind") or "meeting").strip(),
        start=str(args.get("start") or "").strip(),
        duration_minutes=int(args.get("duration_minutes") or 60),
        attendees=attendees,
        location=str(args.get("location") or "").strip(),
    )
    return {
        "created": record,
        "invited": record["attendees"],
        "note": (
            "日程已落本地事务存储；会议临近通知由通知扫描链对创建人与参会人逐人生成"
            "（站内通知口径，不外发真实邮件邀请）"
        ),
    }


def _fmt_min(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


async def _schedule_freebusy(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.schedule.freebusy：人员空闲查询（工作时段 09:00-18:00 减去当日忙碌区间）。"""
    day = _parse_date(args.get("date") or business_now().date().isoformat(), "date")
    persons_raw = args.get("persons")
    persons = (
        [str(p).strip() for p in persons_raw if str(p).strip()]
        if isinstance(persons_raw, list)
        else []
    )
    if not persons:
        persons = [ctx.username]
    data = await load_affairs(ctx.tenant)

    def _on_day(sched: dict[str, Any]) -> bool:
        return sched.get("kind") == "meeting" and str(sched.get("start", "")).startswith(
            day.isoformat()
        )

    result: list[dict[str, Any]] = []
    for person in persons:
        busy: list[tuple[int, int, str]] = []
        for sched in data["schedules"]:
            if not _on_day(sched):
                continue
            if sched.get("owner") != person and person not in (sched.get("attendees") or []):
                continue
            start_dt = _parse_dt(sched["start"], "start")
            end_text = sched.get("end") or sched["start"]
            end_dt = _parse_dt(end_text, "end")
            busy.append(
                (
                    start_dt.hour * 60 + start_dt.minute,
                    max(end_dt.hour * 60 + end_dt.minute, start_dt.hour * 60 + start_dt.minute),
                    str(sched.get("title") or ""),
                )
            )
        busy.sort()
        free: list[dict[str, str]] = []
        cursor = _WORK_START_MIN
        for start_min, end_min, _title in busy:
            if start_min > cursor:
                free.append(
                    {"from": _fmt_min(cursor), "to": _fmt_min(min(start_min, _WORK_END_MIN))}
                )
            cursor = max(cursor, min(end_min, _WORK_END_MIN))
        if cursor < _WORK_END_MIN:
            free.append({"from": _fmt_min(cursor), "to": _fmt_min(_WORK_END_MIN)})
        result.append(
            {
                "person": person,
                "busy": [{"from": _fmt_min(s), "to": _fmt_min(e), "title": t} for s, e, t in busy],
                "free": free,
            }
        )
    return {
        "date": day.isoformat(),
        "work_hours": f"{_fmt_min(_WORK_START_MIN)}-{_fmt_min(_WORK_END_MIN)}",
        "persons": result,
        "source": "local-affairs-store",
        "fetched_at": _now_text(),
    }


async def _worklog_generate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.worklog.generate：个人工作台账（日/周窗口，已完成+待完成+日程聚合直出）。"""
    period = str(args.get("period") or "weekly").strip()
    if period not in ("daily", "weekly"):
        raise BusinessError(ErrorCode.PARAM_INVALID, "period 只能是 daily / weekly")
    end_day = _parse_date(args.get("end_date") or business_now().date().isoformat(), "end_date")
    span = 1 if period == "daily" else 7
    start_day = end_day - timedelta(days=span - 1)
    data = await load_affairs(ctx.tenant)
    mine = [t for t in data["todos"] if t.get("owner") == ctx.username]

    def _in_window(text: str) -> bool:
        return bool(text) and start_day.isoformat() <= text[:10] <= end_day.isoformat()

    done = [t for t in mine if t.get("status") == "done" and _in_window(t.get("completed_at", ""))]
    pending = [
        t
        for t in mine
        if t.get("status") == "open"
        and (not t.get("due_date") or t["due_date"] <= end_day.isoformat())
    ]
    schedules = [
        s
        for s in data["schedules"]
        if (s.get("owner") == ctx.username or ctx.username in (s.get("attendees") or []))
        and _in_window(s.get("start", ""))
    ]
    lines = [
        f"# 个人工作台账（{start_day.isoformat()} ~ {end_day.isoformat()}，{ctx.username}）",
        "",
    ]
    lines.append(f"## 已完成（{len(done)} 项）")
    lines.extend(f"- {t['title']}（完成于 {str(t.get('completed_at', ''))[:10]}）" for t in done)
    if not done:
        lines.append("-（窗口内无已完成待办，留白不编造）")
    lines.append("")
    lines.append(f"## 待完成（{len(pending)} 项）")
    lines.extend(
        f"- {t['title']}（截止 {t.get('due_date') or '未设'}，优先级 {t.get('priority', 'medium')}）"
        for t in pending
    )
    if not pending:
        lines.append("-（无待完成事项）")
    lines.append("")
    lines.append(f"## 日程与节点（{len(schedules)} 条）")
    lines.extend(
        f"- {s['start']} {'[节点]' if s.get('kind') == 'milestone' else '[会议]'} {s['title']}"
        for s in schedules
    )
    if not schedules:
        lines.append("-（窗口内无日程）")
    return {
        "period": period,
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "counts": {"done": len(done), "pending": len(pending), "schedules": len(schedules)},
        "worklog": "\n".join(lines),
        "source": f"local-affairs-store:{ctx.username}",
        "fetched_at": _now_text(),
    }


# ---------------- ToolSpec ----------------


def specs() -> tuple[ToolSpec, ...]:
    """日程域三工具的 ToolSpec（create 写恒送审；freebusy/worklog 读免审）。"""
    return (
        ToolSpec(
            name="office.schedule.create",
            scope=SCOPE_WRITE,
            description="一键创建日程（写动作，恒送审）：meeting 会议（含参会人与时长）或 milestone 项目节点；参会人落库后由通知扫描链发会前提醒（站内口径）",
            params={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "日程主题",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "kind": {
                        "type": "string",
                        "description": "meeting 会议 / milestone 项目节点（缺省 meeting）",
                        "enum": ["meeting", "milestone"],
                    },
                    "start": {
                        "type": "string",
                        "description": "开始时间（业务时区，YYYY-MM-DD HH:MM）",
                        "minLength": 16,
                        "maxLength": 16,
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "时长分钟（0-480，缺省 60；milestone 恒为 0）",
                        "minimum": 0,
                        "maximum": 480,
                    },
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "参会人用户名列表（最多 50 人）",
                    },
                    "location": {"type": "string", "description": "地点", "maxLength": 200},
                },
                "required": ["title", "start"],
                "additionalProperties": False,
            },
            handler=_schedule_create,
            idempotent=True,
            requires_approval=True,
            approval_action="office.schedule.create",
        ),
        ToolSpec(
            name="office.schedule.freebusy",
            scope=SCOPE_READ,
            description="人员空闲时间查询：按日返回每位人员的忙碌区间与空闲区间（工作时段 09:00-18:00 实测计算，不臆测档期）",
            params={
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "查询日期 YYYY-MM-DD（缺省今天）",
                        "minLength": 10,
                        "maxLength": 10,
                    },
                    "persons": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "人员用户名列表（缺省查本人）",
                    },
                },
                "additionalProperties": False,
            },
            handler=_schedule_freebusy,
        ),
        ToolSpec(
            name="office.worklog.generate",
            scope=SCOPE_READ,
            description="个人工作台账：日/周窗口内已完成待办、待完成事项与日程节点聚合直出（本地事务存储实测计数，留白不编造）",
            params={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "统计窗口（daily/weekly，缺省 weekly）",
                        "enum": ["daily", "weekly"],
                    },
                    "end_date": {
                        "type": "string",
                        "description": "窗口截止日 YYYY-MM-DD（缺省今天）",
                        "minLength": 10,
                        "maxLength": 10,
                    },
                },
                "additionalProperties": False,
            },
            handler=_worklog_generate,
        ),
    )


def register_all() -> list[str]:
    """注册日程域工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
