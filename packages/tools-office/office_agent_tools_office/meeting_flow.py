"""会议全流程补充三工具（对齐 PRD §2.5 会前/会中/会后缺口）。

职责：
- office.meeting.materials（office:read）：会前整理会议资料——对 DOCS_DIR 内指定文件
  逐个真实抽取文本（复用 file_read.extract_document 同一口径），汇编资料包（每文件
  字数 + 开头摘录），单文件缺失/超限/解析失败如实标 failed 不炸整包；
- office.meeting.digest（office:read）：会中记录内容梳理——对会议速记文本按关键词
  规则归类出决议/行动项/风险三类要点（只摘录原句不改写），风险共用 meeting 词表；
  实时语音转录需音频基建后置，本工具承接「转录/速记文本 → 要点」这一段；
- office.meeting.followup（office:read）：会后落实跟进——把纪要行动项（task/owner/due）
  与本地事务待办台账（affairs 存储）按标题对账，输出 已完成/进行中/已逾期/已取消/
  未建单 五态，逾期判定用业务时区当天（affairs.business_now），绝不臆造进展。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：三只读免审、纯本地零网络；资料摘录只截断不改写；跟进对账查无即「未建单」，
      不编造落实情况；出参带 source + fetched_at 溯源。
对齐：AGENTS.md §3（分层/降级不 500/溯源）；智能办公Agent 产品需求文档.md §2.5
      （会前资料整理、会中要点梳理、会后落实跟进）、§3.2（会议全流程场景）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from . import affairs
from .file_read import MAX_FILE_BYTES, READ_SUFFIXES, extract_document
from .meeting import RISK_KEYWORDS
from .paths import resolve_under_docs

SCOPE_READ = "office:read"

#: 资料包每文件开头摘录长度（只截断不改写）
_EXCERPT_CHARS = 300
#: 会中要点归类关键词（命中即摘录原句，一行可归多类）
_DECISION_MARKERS = ("决议", "决定", "结论", "通过")
_ACTION_MARKERS = ("行动", "待办", "任务", "跟进", "落实", "负责")


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _str_list(value: Any) -> list[str]:
    """入参转非空字符串列表（非列表或空项一律丢弃）。"""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# ---------------- 会前：整理会议资料 ----------------


def _one_material(filename: str) -> dict[str, Any]:
    """单文件资料抽取（同步 IO，调用方包 asyncio.to_thread）。"""
    try:
        path = resolve_under_docs(filename, READ_SUFFIXES)
        if not path.is_file():
            return {
                "filename": path.name,
                "status": "failed",
                "reason": "文件不存在（请先放入文档工作目录）",
            }
        if path.stat().st_size > MAX_FILE_BYTES:
            return {
                "filename": path.name,
                "status": "failed",
                "reason": "文件过大（超 200KB），请拆分后再整理",
            }
        result = extract_document(path)
    except BusinessError as exc:
        return {"filename": str(filename), "status": "failed", "reason": exc.msg[:200]}
    except (OSError, ValueError) as exc:
        return {
            "filename": str(filename),
            "status": "failed",
            "reason": f"解析失败：{str(exc)[:120]}",
        }
    text = str(result.get("text") or "")
    return {
        "filename": path.name,
        "status": "ok",
        "chars": len(text),
        "excerpt": text[:_EXCERPT_CHARS],
        "truncated": bool(result.get("truncated")),
        "degraded": bool(result.get("degraded")),
        "degraded_reason": str(result.get("degraded_reason") or ""),
    }


async def _meeting_materials(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.meeting.materials：会前资料包汇编（逐文件真实抽取，单文件失败如实标注）。"""
    _ = ctx
    title = str(args.get("title") or "").strip()
    if not title:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 title 不能为空：请传入会议主题")
    files = _str_list(args.get("files"))
    if not files:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, "参数 files 不能为空：请传入 DOCS_DIR 内的会议资料文件名列表"
        )
    topics = _str_list(args.get("topics"))
    materials = list(
        await asyncio.gather(*(asyncio.to_thread(_one_material, name) for name in files))
    )
    ok_items = [m for m in materials if m["status"] == "ok"]
    lines: list[str] = [
        f"# 会议资料包：{title}",
        "",
        f"编制时间：{_now_text()}（逐文件真实抽取汇编）",
    ]
    if topics:
        lines += ["", "## 会议议题（原值）"]
        lines += [f"{i}. {item}" for i, item in enumerate(topics, 1)]
    lines += ["", f"## 资料清单（{len(materials)} 份，成功 {len(ok_items)} 份）"]
    for i, m in enumerate(materials, 1):
        if m["status"] == "ok":
            suffix = (
                "……（仅摘录开头，全文见原文件）"
                if m["truncated"] or m["chars"] > _EXCERPT_CHARS
                else ""
            )
            lines += [
                "",
                f"### {i}. {m['filename']}（{m['chars']} 字）",
                m["excerpt"] + suffix if m["excerpt"] else "（未提取到文本，可能为扫描件或空文件）",
            ]
            if m["degraded_reason"]:
                lines.append(f"（解析降级：{m['degraded_reason']}）")
        else:
            lines += ["", f"### {i}. {m['filename']}（读取失败）", f"原因：{m['reason']}"]
    return {
        "title": title,
        "pack": "\n".join(lines),
        "materials": materials,
        "counts": {"file_total": len(materials), "file_ok": len(ok_items)},
        "degraded": not ok_items,
        "degraded_reason": "全部资料文件读取失败：不编造内容，请按原因逐项处理"
        if not ok_items
        else "",
        "source": "local-docs:" + ",".join(str(m["filename"]) for m in materials),
        "fetched_at": _now_text(),
    }


# ---------------- 会中：记录内容梳理要点 ----------------


def _classify_lines(notes: str) -> tuple[list[str], list[str], list[str]]:
    """按行归类（决议/行动/风险），只摘录原句；一行可命中多类。"""
    decisions: list[str] = []
    actions: list[str] = []
    risks: list[str] = []
    risk_words = tuple(kw for kw, _ in RISK_KEYWORDS)
    for raw in notes.splitlines():
        line = raw.strip().lstrip("#->* \t")
        if not line:
            continue
        if any(marker in line for marker in _DECISION_MARKERS):
            decisions.append(line)
        if any(marker in line for marker in _ACTION_MARKERS):
            actions.append(line)
        if any(word in line for word in risk_words):
            risks.append(line)
    return decisions, actions, risks


async def _meeting_digest(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.meeting.digest：会中速记 → 决议/行动项/风险三类要点（关键词规则，不改写）。"""
    _ = ctx
    notes = str(args.get("notes") or "").strip()
    if not notes:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 notes 不能为空：请传入会议速记/转录文本")
    title = str(args.get("title") or "").strip()
    decisions, actions, risks = await asyncio.to_thread(_classify_lines, notes)
    header = f"# 会议要点：{title}" if title else "# 会议要点"
    lines = [header, "", f"梳理时间：{_now_text()}（关键词规则归类，原句摘录未改写）"]
    lines += ["", f"## 一、决议要点（{len(decisions)} 条）"]
    lines += [f"- {item}" for item in decisions] or ["（未识别到决议表述，留白不编造）"]
    lines += ["", f"## 二、行动项（{len(actions)} 条）"]
    lines += [f"- {item}" for item in actions] or ["（未识别到行动表述，留白不编造）"]
    lines += ["", f"## 三、风险提示（{len(risks)} 条）"]
    lines += [f"- {item}" for item in risks] or ["（未命中风险关键词，留白不编造）"]
    total = len(decisions) + len(actions) + len(risks)
    return {
        "title": title,
        "digest": "\n".join(lines),
        "counts": {"decision": len(decisions), "action": len(actions), "risk": len(risks)},
        "degraded": total == 0,
        "degraded_reason": "速记文本未命中任何要点关键词：不编造要点" if total == 0 else "",
        "source": "input.notes",
    }


# ---------------- 会后：行动项落实跟进 ----------------


def _match_todo(todos: list[dict[str, Any]], task: str) -> dict[str, Any] | None:
    """按标题精确对账（查无返回 None，由调用方如实标「未建单」）。"""
    for todo in todos:
        if str(todo.get("title") or "").strip() == task:
            return todo
    return None


def _item_state(
    item: dict[str, Any], todo: dict[str, Any] | None, today_iso: str
) -> dict[str, Any]:
    """单行动项五态判定：done/overdue/open/cancelled/missing（口径全来自台账原值）。"""
    task = str(item.get("task") or "").strip()
    base: dict[str, Any] = {
        "task": task,
        "owner": str(item.get("owner") or "").strip(),
        "due": str(item.get("due") or "").strip(),
    }
    if todo is None:
        return {**base, "state": "missing", "state_label": "未建单", "todo_id": ""}
    status = str(todo.get("status") or "")
    due = str(todo.get("due_date") or "") or base["due"]
    if status == "done":
        return {
            **base,
            "state": "done",
            "state_label": "已完成",
            "todo_id": todo["id"],
            "completed_at": todo.get("completed_at", ""),
            "due": due,
        }
    if status == "cancelled":
        return {
            **base,
            "state": "cancelled",
            "state_label": "已取消",
            "todo_id": todo["id"],
            "due": due,
        }
    state = "overdue" if due and due < today_iso else "open"
    label = "已逾期" if state == "overdue" else "进行中"
    return {**base, "state": state, "state_label": label, "todo_id": todo["id"], "due": due}


async def _meeting_followup(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.meeting.followup：纪要行动项 × 待办台账对账（落实跟进，查无不臆造）。"""
    items = args.get("action_items")
    if not isinstance(items, list) or not items:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            '参数 action_items 不能为空：形如 [{"task": "事项", "owner": "责任人", "due": "YYYY-MM-DD"}]（与 office.minutes.generate 行动项同形）',
        )
    normalized: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict) or not str(item.get("task") or "").strip():
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                'action_items 元素必须为对象且 task 非空，形如 {"task": "事项", "owner": "责任人", "due": "期限"}',
            )
        normalized.append(
            {
                "task": str(item.get("task") or "").strip(),
                "owner": str(item.get("owner") or "").strip(),
                "due": str(item.get("due") or "").strip(),
            }
        )
    data = await affairs.load_affairs(ctx.tenant)
    today_iso = affairs.business_now().date().isoformat()
    results = [
        _item_state(item, _match_todo(data["todos"], item["task"]), today_iso)
        for item in normalized
    ]
    counts = {
        state: sum(1 for r in results if r["state"] == state)
        for state in ("done", "overdue", "open", "cancelled", "missing")
    }
    lines = [
        "# 会议行动项落实跟进",
        "",
        f"对账时间：{_now_text()}（业务当天 {today_iso}，台账标题精确匹配）",
        "",
        "| 事项 | 责任人 | 期限 | 状态 |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {r['task']} | {r['owner'] or '—'} | {r['due'] or '—'} | {r['state_label']} |"
        for r in results
    ]
    if counts["missing"]:
        lines += [
            "",
            f"未建单 {counts['missing']} 项：请确认后经 office.todo.create 补建（送审落盘），不臆造进展。",
        ]
    return {
        "date": today_iso,
        "items": results,
        "counts": counts,
        "followup": "\n".join(lines),
        "source": f"local-affairs-store:{ctx.tenant}",
        "fetched_at": _now_text(),
    }


# ---------------- ToolSpec ----------------


def specs() -> tuple[ToolSpec, ...]:
    """三个补充工具的 ToolSpec（全读免审：只汇编/摘录/对账，不落库不副作用）。"""
    return (
        ToolSpec(
            name="office.meeting.materials",
            scope=SCOPE_READ,
            description="会前整理会议资料：对 DOCS_DIR 指定文件逐个真实抽取文本汇编资料包（每文件字数+开头摘录），单文件失败如实标注不炸整包",
            params={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "会议主题", "minLength": 1},
                    "files": {
                        "type": "array",
                        "description": "资料文件名列表（DOCS_DIR 内，支持 "
                        + " / ".join(READ_SUFFIXES)
                        + "）",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "topics": {
                        "type": "array",
                        "description": "会议议题（可选，原值列入资料包）",
                        "items": {"type": "string"},
                    },
                },
                "required": ["title", "files"],
                "additionalProperties": False,
            },
            handler=_meeting_materials,
        ),
        ToolSpec(
            name="office.meeting.digest",
            scope=SCOPE_READ,
            description="会中记录梳理要点：对速记/转录文本按关键词规则归类决议/行动项/风险三类（原句摘录不改写），全未命中如实 degraded；实时语音转录需音频基建后置",
            params={
                "type": "object",
                "properties": {
                    "notes": {"type": "string", "description": "会议速记/转录文本", "minLength": 1},
                    "title": {"type": "string", "description": "会议主题（可选，仅做标题）"},
                },
                "required": ["notes"],
                "additionalProperties": False,
            },
            handler=_meeting_digest,
        ),
        ToolSpec(
            name="office.meeting.followup",
            scope=SCOPE_READ,
            description="会后落实跟进：纪要行动项与待办台账按标题对账，输出已完成/进行中/已逾期/已取消/未建单五态（业务时区当天判逾期），查无即未建单不臆造进展",
            params={
                "type": "object",
                "properties": {
                    "action_items": {
                        "type": "array",
                        "description": '行动项列表，形如 [{"task": "事项", "owner": "责任人", "due": "YYYY-MM-DD"}]',
                        "items": {
                            "type": "object",
                            "properties": {
                                "task": {"type": "string", "minLength": 1},
                                "owner": {"type": "string"},
                                "due": {"type": "string"},
                            },
                            "required": ["task"],
                            "additionalProperties": False,
                        },
                        "minItems": 1,
                    },
                },
                "required": ["action_items"],
                "additionalProperties": False,
            },
            handler=_meeting_followup,
        ),
    )


def register_all() -> list[str]:
    """注册三个会议流程补充工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
