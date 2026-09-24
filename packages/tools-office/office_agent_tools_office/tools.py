"""内置办公工具实现（三个工具 handler + 规格 + 注册入口）。

职责：
- office.report.generate：结构化中文日报，纯模板直出，每个数字带溯源标注；
- office.schedule.view：日程视图查询，返回演示数据集，显式标注 source=builtin-demo；
- office.todo.create：创建待办（写动作），幂等 + scope=office:write + requires_approval=True。

链路：__init__.register_all() → registry.register(spec) 三个 ToolSpec；executor.call 执行 handler。
红线：本模块只实现 handler 与 ToolSpec，不触及任何 ORM / FastAPI 对象；
      所有入参校验由内核 validate_args 完成，handler 内只做业务逻辑。
对齐：AGENTS.md §3（分层红线：工具实现纯函数）。
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

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
    """office.report.generate：结构化中文日报，纯模板直出，每个数字带溯源标注。

    数值口径：所有数值由输入原值直出，不调大模型，数值一致率 100%。
    """
    _ = ctx  # 纯本地工具，不依赖执行上下文
    title = str(args.get("title") or "").strip()
    if not title:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 title 不能为空：请传入日报标题")
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
        f"本日报共汇总 {count} 项指标{_PROVENANCE}；全部数值由输入原值直出、未经任何模型改写（口径：数值不可编造，缺数宁可留白不补数）。",
    ]
    return {
        "title": title,
        "report": "\n".join(lines),
        "metric_count": count,
        "numeric_consistency": "100%",
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
    """office.todo.create：创建待办（演示实现，审批通过后由 decide 路径真正执行）。

    本 handler 仅在审批通过后被调用（由 server 的 approval_flow 在批准后以
    提交人身份触发）。演示实现无外部副作用，只返回回执。
    """
    title = str(args.get("title") or "").strip()
    description = str(args.get("description") or "").strip()
    priority = str(args.get("priority") or "medium").strip()
    due_date = str(args.get("due_date") or "").strip()

    return {
        "title": title,
        "description": description,
        "priority": priority,
        "due_date": due_date,
        "owner": ctx.username,
        "created_at": _now_text(),
        "note": "演示实现：待办原值回执，无外部副作用；真实接入时在此落待办存储/通知渠道",
    }


# ---------------- 工具规格 ----------------


def specs() -> tuple[ToolSpec, ...]:
    """三个办公工具的 ToolSpec（与内核契约逐字对齐）。"""
    return (
        ToolSpec(
            name="office.report.generate",
            scope=SCOPE_READ,
            description="生成结构化中文日报：输入标题与指标字典，纯模板直出（不调大模型），每个数字带 input.metrics 溯源标注，数值一致率 100%",
            params={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "日报标题",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "metrics": {
                        "type": "object",
                        "description": '指标字典：{"指标名": 数值}',
                        "additionalProperties": {"type": "number"},
                    },
                },
                "required": ["title", "metrics"],
            },
            handler=_report_generate,
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
    names: list[str] = []
    for spec in specs():
        names.append(register(spec).name)
    return names
