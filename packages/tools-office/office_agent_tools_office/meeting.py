"""会议全流程协作工具（office.meeting.agenda / book / risks，对齐 PRD §2.5 V1.1 第二项）。

职责：
- office.meeting.agenda（office:read）：会前议程生成——主题/目标/议题/参会人/时长模板直出，
  缺板块留白不编造；
- office.meeting.book（office:write + 恒送审 + 幂等）：会议预约——invoke 只落审批单，
  复核人批准后才真正执行（演示实现回执原值，无外部日历副作用）；
- office.meeting.risks（office:read）：风险点抓取——按风险关键词规则扫描纪要文本，
  只报命中项（关键词/级别/原文摘录），无命中 degraded 留白，绝不凭空预警。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：纯本地实现，不触及 ORM / FastAPI；预约 start_time 强格式校验
      （YYYY-MM-DD HH:MM）；风险只摘录原文不生成结论。
对齐：AGENTS.md §3（分层/写动作恒送审/数值不可编造类推文本不编造）；
      智能办公Agent 产品需求文档.md §2.5（会前预约与议程/风险预警新增）、
      §3.2（会议全流程场景——会中实时转录需音频基建，后置；会后复用
      office.minutes.generate + office.task.decompose/commit 链）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

SCOPE_READ = "office:read"
SCOPE_WRITE = "office:write"

#: 风险关键词 → 预警级别（只做摘录分级，不做因果推断）
_RISK_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("阻塞", "高"),
    ("故障", "高"),
    ("安全", "高"),
    ("漏洞", "高"),
    ("投诉", "高"),
    ("延期", "中"),
    ("延迟", "中"),
    ("超期", "中"),
    ("缺人", "中"),
    ("人手不足", "中"),
    ("依赖", "中"),
    ("预算超", "中"),
    ("风险", "低"),
    ("不确定", "低"),
    ("卡点", "低"),
)
_LEVEL_ADVICE: dict[str, str] = {
    "高": "建议立即升级处理：明确负责人与解决时限，会后 24 小时内跟进闭环",
    "中": "建议纳入行动项跟踪：在下次例会前复核进展，必要时调整排期",
    "低": "建议保持观察：在纪要中留痕，后续有变化再升级",
}


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _str_list(value: Any) -> list[str]:
    """入参转非空字符串列表（议题/参会人共用；非列表或空项一律丢弃）。"""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _validate_start_time(value: str) -> str:
    """预约时间强格式：YYYY-MM-DD HH:MM（拒绝含糊时间，不臆造场次）。"""
    text = str(value or "").strip()
    try:
        datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"参数 start_time 必须是 YYYY-MM-DD HH:MM 格式（当前：{value}，例：2026-10-10 14:00）",
        ) from exc
    return text


async def _meeting_agenda(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.meeting.agenda：会前议程模板直出（缺板块留白）。"""
    _ = ctx
    title = str(args.get("title") or "").strip()
    if not title:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 title 不能为空：请传入会议主题")
    objective = str(args.get("objective") or "").strip()
    topics = _str_list(args.get("topics"))
    attendees = _str_list(args.get("attendees"))
    duration = args.get("duration_minutes", 60)
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 duration_minutes 必须是正整数（分钟）")
    lines: list[str] = [
        f"# 会议议程：{title}",
        "",
        f"编制时间：{_now_text()}（模板直出，内容均为入参原值）",
        f"预计时长：{duration} 分钟",
        "",
        "## 一、会议目标",
        objective or "（未提供会议目标，留白不编造）",
        "",
        f"## 二、议题安排（{len(topics)} 项）",
    ]
    lines += [f"{i}. {item}" for i, item in enumerate(topics, 1)] or ["（未提供议题，留白不编造）"]
    lines += ["", f"## 三、参会人（{len(attendees)} 人）"]
    lines.append("、".join(attendees) if attendees else "（未提供参会人名单，留白不编造）")
    return {
        "title": title,
        "agenda": "\n".join(lines),
        "counts": {"topic": len(topics), "attendee": len(attendees)},
    }


async def _meeting_book(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.meeting.book：会议预约（演示实现，审批通过后由 decide 路径真正执行）。

    本 handler 仅在审批通过后被调用。演示实现无外部日历副作用，只返回回执。
    """
    title = str(args.get("title") or "").strip()
    if not title:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 title 不能为空：请传入会议主题")
    start_time = _validate_start_time(args.get("start_time"))
    duration = args.get("duration_minutes", 60)
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 duration_minutes 必须是正整数（分钟）")
    attendees = _str_list(args.get("attendees"))
    agenda = str(args.get("agenda") or "").strip()
    return {
        "title": title,
        "start_time": start_time,
        "duration_minutes": duration,
        "attendees": attendees,
        "agenda": agenda,
        "owner": ctx.username,
        "created_at": _now_text(),
        "note": "演示实现：预约原值回执，无外部日历副作用；真实接入时在此落日历系统/发会议邀请",
    }


def _excerpt(text: str, keyword: str, radius: int = 20) -> str:
    """风险摘录：关键词命中处前后各取 radius 字（只摘录不改写）。"""
    pos = text.find(keyword)
    if pos < 0:
        return ""
    start = max(0, pos - radius)
    return text[start : pos + len(keyword) + radius].replace("\n", " ").strip()


async def _meeting_risks(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.meeting.risks：按关键词规则抓风险点（只报命中，无命中 degraded）。"""
    _ = ctx
    minutes = str(args.get("minutes") or "").strip()
    if not minutes:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 minutes 不能为空：请传入会议纪要文本")
    hits: list[dict[str, str]] = []
    seen: set[str] = set()
    for keyword, level in _RISK_KEYWORDS:
        if keyword in minutes and keyword not in seen:
            seen.add(keyword)
            hits.append(
                {
                    "keyword": keyword,
                    "level": level,
                    "excerpt": _excerpt(minutes, keyword),
                    "advice": _LEVEL_ADVICE[level],
                }
            )
    high = sum(1 for hit in hits if hit["level"] == "高")
    return {
        "risks": hits,
        "count": len(hits),
        "high_count": high,
        "degraded": not hits,
        "degraded_reason": (
            "纪要中未命中风险关键词：暂无可预警事项，不编造风险" if not hits else ""
        ),
        "keyword_count": len(_RISK_KEYWORDS),
        "source": "input.minutes",
    }


def specs() -> tuple[ToolSpec, ...]:
    """三个会议工具的 ToolSpec（agenda/risks 读免审，book 写恒送审）。"""
    return (
        ToolSpec(
            name="office.meeting.agenda",
            scope=SCOPE_READ,
            description="生成会前议程：目标/议题/参会人/时长模板直出（不调大模型），未提供的板块留白不编造",
            params={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "会议主题", "minLength": 1},
                    "objective": {"type": "string", "description": "会议目标"},
                    "topics": {
                        "type": "array",
                        "description": "议题列表",
                        "items": {"type": "string"},
                    },
                    "attendees": {
                        "type": "array",
                        "description": "参会人名单",
                        "items": {"type": "string"},
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "预计时长（分钟，默认 60）",
                    },
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            handler=_meeting_agenda,
        ),
        ToolSpec(
            name="office.meeting.book",
            scope=SCOPE_WRITE,
            description="预约会议（写动作）：幂等 + scope=office:write + 恒送审，invoke 只落审批单，审批通过后由复核人触发执行",
            params={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "会议主题", "minLength": 1},
                    "start_time": {
                        "type": "string",
                        "description": "开始时间（YYYY-MM-DD HH:MM，如 2026-10-10 14:00）",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "时长（分钟，默认 60）",
                    },
                    "attendees": {
                        "type": "array",
                        "description": "参会人名单",
                        "items": {"type": "string"},
                    },
                    "agenda": {"type": "string", "description": "关联议程摘要"},
                },
                "required": ["title", "start_time"],
                "additionalProperties": False,
            },
            handler=_meeting_book,
            idempotent=True,
            requires_approval=True,
            approval_action="office.meeting.book",
        ),
        ToolSpec(
            name="office.meeting.risks",
            scope=SCOPE_READ,
            description="从纪要文本抓风险点：关键词规则命中才预警（关键词/级别/原文摘录），无命中如实 degraded，绝不凭空预警",
            params={
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "string",
                        "description": "会议纪要文本",
                        "minLength": 1,
                    },
                },
                "required": ["minutes"],
                "additionalProperties": False,
            },
            handler=_meeting_risks,
        ),
    )


def register_all() -> list[str]:
    """注册三个会议工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
