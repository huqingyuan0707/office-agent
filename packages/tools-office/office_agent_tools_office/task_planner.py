"""任务拆解工具（office.task.decompose / office.task.commit，对齐 PRD §6）。

职责：
- office.task.decompose（office:read）：规则拆解器——按通用项目生命周期把大目标拆成
  结构化子任务（标题/交付物/责任人/期限/优先级/依赖）；参与人缺失或工期缺失时
  **不臆造**：对应字段留空并在 missing_info 里给出可照做的追问提示（PRD §6.5.1）；
- office.task.commit（office:write + 恒送审 + 幂等键必带）：确认后的批量建单，
  审批闸门即 PRD §6.5.4 的二次确认，复核人批准后才真正执行。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：纯本地实现，不触及 ORM / FastAPI；责任人只从入参参与人中识别，绝不凭空造人。
对齐：AGENTS.md §3（分层红线/写动作恒送审）；智能办公Agent 产品需求文档.md §6（任务拆解能力详述）。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"
SCOPE_WRITE = "office:write"

_MAX_SUBTASKS = 50

#: 通用项目生命周期拆解模板（领域无关；owner_roles 为各阶段候选角色关键词）
_PHASES: list[dict[str, Any]] = [
    {
        "title": "需求调研与方案定稿",
        "deliverable": "需求文档",
        "owner_roles": ("产品", "项目", "业务"),
    },
    {
        "title": "设计与原型确认",
        "deliverable": "设计稿与实施方案",
        "owner_roles": ("设计", "UI", "产品"),
    },
    {
        "title": "开发与实施",
        "deliverable": "可运行的功能交付物",
        "owner_roles": ("开发", "前端", "后端", "工程", "技术"),
    },
    {"title": "测试与验收", "deliverable": "测试与验收报告", "owner_roles": ("测试", "QA", "质量")},
    {
        "title": "发布与交付",
        "deliverable": "上线发布与交付清单",
        "owner_roles": ("产品", "开发", "运维", "项目"),
    },
]


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _match_owner(role_keywords: tuple[str, ...], participants: list[str]) -> str:
    """从参与人中按角色关键词匹配责任人（双向包含，如「产品经理」含「产品」）。"""
    for participant in participants:
        low = participant.lower()
        if any(keyword in low or low in keyword for keyword in role_keywords):
            return participant
    return ""


def _due_for_phase(phase_index: int, phases: int, duration_weeks: int, start_date: str) -> str:
    """按等分周分配阶段截止：给 start_date 算实际日期，否则输出「第 N 周内」。"""
    week_end = round((phase_index + 1) * duration_weeks / phases)
    if start_date:
        try:
            base = date.fromisoformat(start_date)
        except ValueError:
            return f"第{week_end}周内"
        return (base + timedelta(days=week_end * 7 - 1)).isoformat()
    return f"第{week_end}周内"


def _validate_date(value: str) -> str:
    """start_date 口径校验：给了就必须是 YYYY-MM-DD（拒绝含糊日期）。"""
    if not value:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 start_date 必须是 YYYY-MM-DD 格式（当前：{value}）"
        ) from exc
    return value


async def _task_decompose(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.task.decompose：规则拆解器，缺人/缺期不臆造，输出可确认的子任务清单。"""
    _ = ctx
    goal = str(args.get("goal") or "").strip()
    if not goal:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 goal 不能为空：请描述要拆解的大目标")
    duration_weeks = args.get("duration_weeks")
    if duration_weeks is not None and (
        not isinstance(duration_weeks, int)
        or isinstance(duration_weeks, bool)
        or not 1 <= duration_weeks <= 52
    ):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 duration_weeks 必须是 1-52 的整数（周）")
    start_date = _validate_date(str(args.get("start_date") or "").strip())
    participants_raw = args.get("participants")
    participants: list[str] = []
    if isinstance(participants_raw, list):
        participants = [str(p).strip() for p in participants_raw if str(p).strip()][:20]

    missing_info: list[str] = []
    if not participants:
        missing_info.append(
            "参与人：请补充这个项目由哪些角色/人员参与（如：产品、设计、前端、测试）"
        )
    if duration_weeks is None:
        missing_info.append("工期：请补充总工期（多少周），以及期望的启动日期（可选）")

    phases = len(_PHASES)
    subtasks: list[dict[str, Any]] = []
    unmatched_phases: list[str] = []
    for index, phase in enumerate(_PHASES):
        owner = _match_owner(phase["owner_roles"], participants) if participants else ""
        if participants and not owner:
            unmatched_phases.append(phase["title"])
        subtasks.append(
            {
                "id": f"task-{index + 1}",
                "title": phase["title"],
                "deliverable": phase["deliverable"],
                "owner": owner,
                "due_date": (
                    _due_for_phase(index, phases, duration_weeks, start_date)
                    if duration_weeks is not None
                    else ""
                ),
                "priority": "high",
                "dependencies": [] if index == 0 else [f"task-{index}"],
            }
        )
    if unmatched_phases:
        missing_info.append(
            "以下阶段未能从参与人中识别出责任人（已留空，请对话指派）："
            + "、".join(unmatched_phases)
        )

    return {
        "goal": goal,
        "subtasks": subtasks,
        "total": len(subtasks),
        "missing_info": missing_info,
        "next_hint": (
            "请核对/对话调整上述清单；确认无误后调用 office.task.commit 批量建单"
            "（tasks 数组 + idem_key，需复核人审批后执行）"
        ),
        "source": "rule-based-planner",
        "decomposed_at": _now_text(),
    }


async def _task_commit(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.task.commit：批量建单回执（审批通过后由 decide 路径触发执行）。

    演示实现无外部副作用，只返回建单回执；真实接入时在此落待办存储并推送责任人通知。
    """
    idem_key = str(args.get("idem_key") or "").strip()
    tasks_raw = args.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 tasks 必须为非空数组（每项含 title）")
    if len(tasks_raw) > _MAX_SUBTASKS:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"单次最多创建 {_MAX_SUBTASKS} 条任务（当前 {len(tasks_raw)} 条）",
        )

    created: list[dict[str, str]] = []
    for index, item in enumerate(tasks_raw, 1):
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            raise BusinessError(
                ErrorCode.PARAM_INVALID, f"第 {index} 项任务缺少 title（不臆造任务标题）"
            )
        owner = str(item.get("owner") or "").strip()
        created.append(
            {
                "title": str(item["title"]).strip(),
                "owner": owner,
                "due_date": str(item.get("due_date") or "").strip(),
                "priority": str(item.get("priority") or "medium").strip(),
                "description": str(item.get("description") or "").strip(),
                "notified": "true" if owner else "false",
            }
        )
    notified_owners = sorted({item["owner"] for item in created if item["owner"]})
    return {
        "idem_key": idem_key,
        "created": created,
        "count": len(created),
        "notified_owners": notified_owners,
        "note": (
            "演示实现：待办原值回执，无外部副作用；真实接入时在此落待办存储/通知渠道。"
            "未指派责任人的任务不发送通知（不臆造接收人）"
        ),
        "committed_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """任务拆解两工具的 ToolSpec。"""
    return (
        ToolSpec(
            name="office.task.decompose",
            scope=SCOPE_READ,
            description="任务拆解：把大目标按通用项目生命周期拆成结构化子任务（标题/交付物/责任人/期限/优先级/依赖）；参与人与工期缺失时对应字段留空并给出追问提示，绝不臆造",
            params={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "要拆解的大目标/项目描述",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "duration_weeks": {
                        "type": "integer",
                        "description": "总工期（周，1-52，缺省时截止时间留空并追问）",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "启动日期（YYYY-MM-DD，可选）",
                        "maxLength": 10,
                    },
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "参与人/角色列表（缺省时责任人留空并追问）",
                    },
                },
                "required": ["goal"],
                "additionalProperties": False,
            },
            handler=_task_decompose,
        ),
        ToolSpec(
            name="office.task.commit",
            scope=SCOPE_WRITE,
            description="批量创建任务（写动作）：恒送审 + idem_key 必填——审批闸门即批量建单的二次确认，复核人批准后才执行；未指派责任人的任务不发送通知",
            params={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": '任务数组，元素形如 {"title": 必填, "owner": "责任人", "due_date": "YYYY-MM-DD", "priority": "low/medium/high", "description": "备注"}',
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                                "owner": {"type": "string", "maxLength": 64},
                                "due_date": {"type": "string", "maxLength": 10},
                                "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                                "description": {"type": "string", "maxLength": 500},
                            },
                            "required": ["title"],
                        },
                    },
                    "idem_key": {
                        "type": "string",
                        "description": "幂等键（8-64 字符，重复提交不重复建单）",
                        "minLength": 8,
                        "maxLength": 64,
                    },
                },
                "required": ["tasks", "idem_key"],
                "additionalProperties": False,
            },
            handler=_task_commit,
            idempotent=True,
            requires_approval=True,
            approval_action="office.task.commit",
        ),
    )


def register_all() -> list[str]:
    """注册任务拆解工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
