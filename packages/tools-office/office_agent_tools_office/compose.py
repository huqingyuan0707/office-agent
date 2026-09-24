"""通用文案起草工具（office.memo.compose，对齐 PRD §2.1 自动生成）。

职责：office.memo.compose（office:read）——通知公告 / 邮件 / 方案草稿 / 工作总结 /
      工作汇报五类通用文案模板直出（不调大模型）；正文只排输入原值给的要点，
      未给的可选板块留白不编造。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：纯本地实现，不触及 ORM / FastAPI；周期性汇报（日/周/月报）归
      office.report.generate，本工具只做非周期通用文案，职责不重叠。
对齐：AGENTS.md §3（分层红线：工具实现纯函数）；智能办公Agent 产品需求文档.md §2.1
      （自动生成：通知公告/邮件/方案草稿/工作总结/工作汇报）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

SCOPE_READ = "office:read"

#: 五类文案模板：中文名 + 板块渲染顺序（header/points/footer 均来自入参原值）
_KIND_LABELS: dict[str, str] = {
    "notice": "通知公告",
    "email": "邮件",
    "proposal": "方案草稿",
    "summary": "工作总结",
    "briefing": "工作汇报",
}


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _kind_or_raise(kind: str) -> str:
    """文案类型口径：只认五类（未知名中文可操作报错）。"""
    name = str(kind or "").strip()
    if name not in _KIND_LABELS:
        valid = "、".join(sorted(_KIND_LABELS))
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 kind 只能是 {valid}（当前：{kind}）")
    return name


def _points_or_raise(points: Any) -> list[str]:
    """正文要点口径：非空字符串数组（空文案拒绝，不编造正文）。"""
    if not isinstance(points, list):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 points 必须是非空数组")
    items = [str(item).strip() for item in points if str(item).strip()]
    if not items:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 points 不能为空：请给出正文要点")
    return items[:50]


async def _memo_compose(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.memo.compose：五类通用文案模板直出（缺板块留白）。"""
    _ = ctx
    kind = _kind_or_raise(args.get("kind"))
    title = str(args.get("title") or "").strip()
    if not title:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 title 不能为空：请传入文案标题")
    points = _points_or_raise(args.get("points"))
    header = args.get("header") or {}
    if not isinstance(header, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 header 必须是键值对对象")
    to_who = str(header.get("to") or "").strip()
    from_who = str(header.get("from") or "").strip()
    date = str(header.get("date") or "").strip()
    footer = str(args.get("footer") or "").strip()

    lines: list[str] = [
        f"# {_KIND_LABELS[kind]}：{title}",
        "",
        f"起草时间：{_now_text()}（模板直出，内容均为入参原值）",
    ]
    if to_who:
        lines.append(f"致：{to_who}")
    if from_who:
        lines.append(f"拟稿：{from_who}")
    if date:
        lines.append(f"日期：{date}")
    lines += ["", "## 正文"]
    lines += [f"{i}. {item}" for i, item in enumerate(points, 1)]
    lines += ["", "## 落款"]
    lines.append(footer or "（未提供落款，留白不编造）")
    return {
        "kind": kind,
        "kind_label": _KIND_LABELS[kind],
        "document": "\n".join(lines),
        "point_count": len(points),
    }


def specs() -> tuple[ToolSpec, ...]:
    """通用文案工具的 ToolSpec（读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.memo.compose",
            scope=SCOPE_READ,
            description="起草通用办公文案：通知公告/邮件/方案草稿/工作总结/工作汇报五类模板直出（不调大模型），正文只排入参要点，未给板块留白不编造",
            params={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "文案类型（notice/email/proposal/summary/briefing）",
                        "enum": ["notice", "email", "proposal", "summary", "briefing"],
                    },
                    "title": {"type": "string", "description": "文案标题", "minLength": 1},
                    "points": {
                        "type": "array",
                        "description": "正文要点数组（非空，原值直排）",
                        "items": {"type": "string"},
                    },
                    "header": {
                        "type": "object",
                        "description": "抬头（to/from/date 可选键）",
                    },
                    "footer": {"type": "string", "description": "落款（缺省留白）"},
                },
                "required": ["kind", "title", "points"],
                "additionalProperties": False,
            },
            handler=_memo_compose,
        ),
    )


def register_all() -> list[str]:
    """注册通用文案工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
