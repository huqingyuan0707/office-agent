"""内置办公工具实现（文案生成 / 日程 / 待办 的 handler + 规格）。

职责：
- office.report.generate：结构化中文日报/周报/月报（report_type=daily/weekly/monthly），纯模板直出，每个数字带溯源标注；
- office.minutes.generate：会议纪要（议题/决议/行动项模板直出，缺的板块留白不编造）；
- office.schedule.view：日程视图查询，返回演示数据集，显式标注 source=builtin-demo；
- office.todo.create：创建待办（写，恒送审），审批通过后落 affairs 本地事务存储（PRD §2.2）。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：本模块只实现 handler 与 ToolSpec，不触及任何 ORM / FastAPI 对象；
      所有入参校验由内核 validate_args 完成，handler 内只做业务逻辑；
      内容全部由入参原值直出（数值不可编造，缺数留白）。
对齐：AGENTS.md §3（分层红线：工具实现纯函数）；
      智能办公Agent 产品需求文档.md §5.1（V1.0 文案生成：周报/纪要）。
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from . import affairs

#: Scope 常量（office 域与根级 office_agent/contracts.py 对齐）
SCOPE_READ = "office:read"
SCOPE_WRITE = "office:write"

_PROVENANCE = "(来源: input.metrics)"

#: 内置演示日程（无外部数据源，演示用）
_DEMO_SCHEDULES: list[dict[str, Any]] = [
    {
        "date": "2026-09-22",
        "items": [{"time": "09:00", "title": "周会"}, {"time": "14:00", "title": "项目评审"}],
    },
    {"date": "2026-09-23", "items": [{"time": "10:00", "title": "客户拜访"}]},
    {"date": "2026-09-24", "items": [{"time": "15:00", "title": "1v1 谈话"}]},
]


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------- 三个工具 handler ----------------


async def _report_generate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.report.generate：结构化中文日报/周报/月报，纯模板直出，每个数字带溯源标注。

    数值口径：所有数值由输入原值直出，不调大模型，数值一致率 100%；
    周报（report_type=weekly）追加「本周亮点 / 下周计划」两个板块（入参未给则留白说明）；
    月报（report_type=monthly）追加「本月重点 / 下月计划」两个板块（复用 highlights /
    next_plan 入参，渲染时按月口径标注，缺省同样留白）。
    """
    _ = ctx  # 纯本地工具，不依赖执行上下文
    title = str(args.get("title") or "").strip()
    if not title:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 title 不能为空：请传入报告标题")
    report_type = str(args.get("report_type") or "daily").strip()
    metrics = args.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, '参数 metrics 必须为非空对象，形如 {"指标名": 数值}'
        )

    lines: list[str] = [
        f"# {title}",
        "",
        f"生成时间：{_now_text()}（纯模板直出，未调用任何大模型）",
        "",
        "## 一、核心指标",
    ]
    count = 0
    for key, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"指标「{key}」的值必须是数字（当前类型 {type(value).__name__}）",
            )
        count += 1
        lines.append(f"- {key}：{value}{_PROVENANCE}")
    lines += [
        "",
        "## 二、小结",
        f"共汇总 {count} 项指标{_PROVENANCE}；全部数值由输入原值直出、未经任何模型改写"
        "（口径：数值不可编造，缺数宁可留白不补数）。",
    ]
    if report_type == "weekly":
        lines.insert(3, "报告类型：周报")
        highlights = _str_list(args.get("highlights"))
        next_plan = _str_list(args.get("next_plan"))
        lines += ["", "## 三、本周亮点"]
        lines += (
            [f"- {item}" for item in highlights]
            if highlights
            else ["-（本周亮点：未提供，留白不编造）"]
        )
        lines += ["", "## 四、下周计划"]
        lines += (
            [f"- {item}" for item in next_plan]
            if next_plan
            else ["-（下周计划：未提供，留白不编造）"]
        )
    elif report_type == "monthly":
        lines.insert(3, "报告类型：月报")
        highlights = _str_list(args.get("highlights"))
        next_plan = _str_list(args.get("next_plan"))
        lines += ["", "## 三、本月重点"]
        lines += (
            [f"- {item}" for item in highlights]
            if highlights
            else ["-（本月重点：未提供，留白不编造）"]
        )
        lines += ["", "## 四、下月计划"]
        lines += (
            [f"- {item}" for item in next_plan]
            if next_plan
            else ["-（下月计划：未提供，留白不编造）"]
        )
    return {
        "title": title,
        "report_type": report_type,
        "report": "\n".join(lines),
        "metric_count": count,
        "numeric_consistency": "100%",
    }


def _str_list(value: Any) -> list[str]:
    """入参转非空字符串列表（周报亮点/计划等板块共用；非列表或空项一律丢弃）。"""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


async def _minutes_generate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.minutes.generate：会议纪要模板直出（议题/决议/行动项，缺板块留白）。"""
    _ = ctx
    title = str(args.get("title") or "").strip()
    if not title:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 title 不能为空：请传入会议主题")
    attendees = _str_list(args.get("attendees"))
    agenda = _str_list(args.get("agenda"))
    decisions = _str_list(args.get("decisions"))
    action_items = args.get("action_items")
    actions: list[dict[str, str]] = []
    if isinstance(action_items, list):
        for item in action_items:
            if not isinstance(item, dict):
                raise BusinessError(
                    ErrorCode.PARAM_INVALID,
                    '参数 action_items 元素必须为对象，形如 {"task": "事项", "owner": "责任人", "due": "期限"}',
                )
            task = str(item.get("task") or "").strip()
            if not task:
                raise BusinessError(
                    ErrorCode.PARAM_INVALID, "行动项 task 不能为空（不臆造事项内容）"
                )
            actions.append(
                {
                    "task": task,
                    "owner": str(item.get("owner") or "").strip(),
                    "due": str(item.get("due") or "").strip(),
                }
            )

    lines: list[str] = [
        f"# 会议纪要：{title}",
        "",
        f"记录时间：{_now_text()}（模板直出，内容均为入参原值）",
        "",
        f"## 一、参会人（{len(attendees)} 人）",
    ]
    lines.append("、".join(attendees) if attendees else "（未提供参会人名单，留白不编造）")
    lines += ["", f"## 二、会议议题（{len(agenda)} 项）"]
    lines += [f"{i}. {item}" for i, item in enumerate(agenda, 1)] or ["（未提供议题，留白不编造）"]
    lines += ["", f"## 三、会议决议（{len(decisions)} 条）"]
    lines += [f"- {item}" for item in decisions] or ["（未提供决议，留白不编造）"]
    lines += ["", f"## 四、行动项（{len(actions)} 条）"]
    if actions:
        lines += [
            f"- {a['task']}｜责任人：{a['owner'] or '待定'}｜期限：{a['due'] or '待定'}"
            for a in actions
        ]
    else:
        lines.append("（未提供行动项，留白不编造）")
    return {
        "title": title,
        "minutes": "\n".join(lines),
        "counts": {
            "attendee": len(attendees),
            "agenda": len(agenda),
            "decision": len(decisions),
            "action_item": len(actions),
        },
    }


async def _schedule_view(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.schedule.view：日程视图查询（演示实现，返回内置演示数据集）。"""
    _ = ctx
    view_type = str(args.get("view_type") or "weekly").strip()
    start_date = str(args.get("start_date") or "").strip()
    schedules = copy.deepcopy(_DEMO_SCHEDULES)
    return {
        "view_type": view_type,
        "start_date": start_date,
        "count": len(schedules),  # 真实计数：供下游日报规则取值，杜绝模板里硬编码数字
        "source": "builtin-demo",
        "fetched_at": _now_text(),
        "schedules": schedules,
    }


async def _todo_create(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.todo.create：创建待办（写动作，审批通过后由 decide 路径真正落盘）。

    落盘走 affairs 本地事务存储（PRD §2.2），到期提醒由 server 通知扫描链按 due_date 生成。
    """
    title = str(args.get("title") or "").strip()
    description = str(args.get("description") or "").strip()
    priority = str(args.get("priority") or "medium").strip()
    due_date = str(args.get("due_date") or "").strip()

    record = await affairs.add_todo(
        ctx.tenant,
        ctx.username,
        title=title,
        description=description,
        priority=priority,
        due_date=due_date,
    )
    return {
        **record,
        "note": "待办已落本地事务存储（office.todo.list 可查）；设了截止日期会进到期提醒扫描",
    }


# ---------------- 工具规格 ----------------


def specs() -> tuple[ToolSpec, ...]:
    """三个办公工具的 ToolSpec（与内核契约逐字对齐）。"""
    return (
        ToolSpec(
            name="office.report.generate",
            scope=SCOPE_READ,
            description="生成结构化中文日报/周报/月报：输入标题与指标字典，纯模板直出（不调大模型），每个数字带 input.metrics 溯源标注，数值一致率 100%；周报可附本周亮点与下周计划，月报可附本月重点与下月计划（同 highlights/next_plan 入参）",
            params={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "报告标题",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "report_type": {
                        "type": "string",
                        "description": "报告类型（daily 日报 / weekly 周报 / monthly 月报，缺省日报）",
                        "enum": ["daily", "weekly", "monthly"],
                    },
                    "metrics": {
                        "type": "object",
                        "description": '指标字典：{"指标名": 数值}',
                        "additionalProperties": {"type": "number"},
                    },
                    "highlights": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "亮点（周报渲染为本周亮点/月报渲染为本月重点，缺省留白）",
                    },
                    "next_plan": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "后续计划（周报渲染为下周计划/月报渲染为下月计划，缺省留白）",
                    },
                },
                "required": ["title", "metrics"],
            },
            handler=_report_generate,
        ),
        ToolSpec(
            name="office.minutes.generate",
            scope=SCOPE_READ,
            description="生成会议纪要：参会人/议题/决议/行动项模板直出（不调大模型），未提供的板块留白不编造；行动项含责任人与期限",
            params={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "会议主题",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "参会人名单",
                    },
                    "agenda": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "会议议题列表",
                    },
                    "decisions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "会议决议列表",
                    },
                    "action_items": {
                        "type": "array",
                        "description": '行动项数组，元素形如 {"task": "事项", "owner": "责任人", "due": "期限"}',
                        "items": {
                            "type": "object",
                            "properties": {
                                "task": {"type": "string"},
                                "owner": {"type": "string"},
                                "due": {"type": "string"},
                            },
                            "required": ["task"],
                        },
                    },
                },
                "required": ["title"],
            },
            handler=_minutes_generate,
        ),
        ToolSpec(
            name="office.schedule.view",
            scope=SCOPE_READ,
            description="查询日程视图：按视图类型（weekly/daily）和起始日期返回日程卡片（演示实现，响应显式标注 source=builtin-demo）",
            params={
                "type": "object",
                "properties": {
                    "view_type": {
                        "type": "string",
                        "description": "视图类型（weekly / daily / monthly）",
                        "enum": ["weekly", "daily", "monthly"],
                    },
                    "start_date": {
                        "type": "string",
                        "description": "起始日期（YYYY-MM-DD）",
                        "minLength": 10,
                        "maxLength": 10,
                    },
                },
                "additionalProperties": False,
            },
            handler=_schedule_view,
        ),
        ToolSpec(
            name="office.todo.create",
            scope=SCOPE_WRITE,
            description="创建待办（写动作）：幂等 + scope=office:write + 恒送审，invoke 只落审批单，审批通过后由复核人触发执行",
            params={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "待办标题",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "description": {"type": "string", "description": "待办详情", "maxLength": 1000},
                    "priority": {
                        "type": "string",
                        "description": "优先级",
                        "enum": ["low", "medium", "high"],
                    },
                    "due_date": {
                        "type": "string",
                        "description": "截止日期（YYYY-MM-DD）",
                        "minLength": 10,
                        "maxLength": 10,
                    },
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            handler=_todo_create,
            idempotent=True,
            requires_approval=True,
            approval_action="office.todo.create",
        ),
    )


def register_all() -> list[str]:
    """把三个办公工具注册进内核注册中心；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
