"""群消息摘要（office.im.digest，对齐 PRD §2.7「飞书/企业微信群消息定时摘要」）。

职责（office:read，免审，入参驱动——不接真实 IM 消息源，杜绝假数据源）：
- 入参 messages（1-100 条 {sender, text, time?}）+ me（本人称呼）+ source
  （feishu/wechat/generic，仅信息性标注）；
- 出参：总数/分人计数 + @本人条目逐条摘录 + @本人任务候选（@me 句子含 mail
  动作词，责任人即本人，截止复用 mail 日期正则，提不出置 null）+
  关键问句（？/?）/决议（决定/确定/结论/通过）/风险（风险/注意/阻塞）三类
  关键词摘录 + 中文摘要 brief。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：只摘录原文不推断；动作词/日期正则复用 mail 同一实现（零复制）；
      空消息流直接 1001 拒绝（不定出空摘要冒充结论）。
如实后置：定时调度需 §2.11 定时任务框架（本仓无 scheduler），本工具为按需摘要；
      真实群消息源需经 linkage 白名单工具接入后才有真数据可定时投喂。
对齐：AGENTS.md §3（降级不 500/溯源/不编造）；智能办公Agent 产品需求文档.md §2.7。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from . import mail

SCOPE_READ = "office:read"

_MAX_MESSAGES = 100
_MAX_TEXT = 2000
_MAX_BRIEF_ITEMS = 8

_RE_MENTION = re.compile(r"@([\u4e00-\u9fa5A-Za-z]{2,20})")
_RE_QUESTION = re.compile(r"[?？]")
_RE_DECISION = re.compile(r"决定|确定|结论|通过|就这么定")
_RE_RISK = re.compile(r"风险|注意|阻塞|延期|超期")


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _messages_or_raise(raw: Any) -> list[dict[str, str]]:
    """消息口径：1-100 条 {sender, text}，单条文本 ≤2000 字（超长拒绝不截断编造）。"""
    if not isinstance(raw, list) or not raw:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            '参数 messages 必须是非空数组，元素形如 {"sender": "张三", "text": "…"}',
        )
    if len(raw) > _MAX_MESSAGES:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 messages 最多 {_MAX_MESSAGES} 条")
    cleaned: list[dict[str, str]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise BusinessError(ErrorCode.PARAM_INVALID, f"第 {index} 条必须是对象")
        sender = str(item.get("sender") or "").strip()
        text = str(item.get("text") or "").strip()
        if not sender or not text:
            raise BusinessError(ErrorCode.PARAM_INVALID, f"第 {index} 条 sender/text 均不能为空")
        if len(text) > _MAX_TEXT:
            raise BusinessError(ErrorCode.PARAM_INVALID, f"第 {index} 条文本最长 {_MAX_TEXT} 字")
        cleaned.append({"sender": sender, "text": text, "time": str(item.get("time") or "")})
    return cleaned


def _sentences(text: str) -> list[str]:
    """按句切分（保留？? 在句内——问句识别靠它，不能像 mail 行动项那样切掉）。"""
    return [part.strip(" ，,：:") for part in re.split(r"[。！！;\n；]+", text) if part.strip()]


def _scan_one(msg: dict[str, str], index: int, me: str) -> dict[str, Any]:
    """单条消息扫描：@本人摘录 + 任务候选 + 三类关键词摘录（原文直出）。"""
    hit_me = me in _RE_MENTION.findall(msg["text"]) or f"@{me}" in msg["text"]
    found: dict[str, Any] = {
        "mentions": [],
        "tasks": [],
        "questions": [],
        "decisions": [],
        "risks": [],
    }
    for sent in _sentences(msg["text"]):
        if not sent:
            continue
        if hit_me and (me in sent or f"@{me}" in sent):
            found["mentions"].append({"index": index, "sender": msg["sender"], "text": sent})
            if any(verb in sent for verb in mail._ACTION_VERBS):
                due = mail._RE_DUE.search(sent)
                found["tasks"].append(
                    {
                        "task": sent[:120],
                        "from": msg["sender"],
                        "due": due.group(0) if due else None,
                    }
                )
        if _RE_QUESTION.search(sent):
            found["questions"].append(sent[:120])
        if _RE_DECISION.search(sent):
            found["decisions"].append(sent[:120])
        if _RE_RISK.search(sent):
            found["risks"].append(sent[:120])
    return found


async def _im_digest(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.im.digest：群消息摘要（@本人任务 + 三类关键信息摘录 + brief）。"""
    _ = ctx
    messages = _messages_or_raise(args.get("messages"))
    me = str(args.get("me") or "").strip()
    if not me:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 me 不能为空：请传入本人在群里的称呼")
    source = str(args.get("source") or "generic").strip()
    if source not in ("feishu", "wechat", "generic"):
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 source 只能是 feishu/wechat/generic（当前：{source}）"
        )
    by_sender: dict[str, int] = {}
    mentions: list[dict[str, str]] = []
    my_tasks: list[dict[str, Any]] = []
    questions: list[str] = []
    decisions: list[str] = []
    risks: list[str] = []
    for index, msg in enumerate(messages, 1):
        by_sender[msg["sender"]] = by_sender.get(msg["sender"], 0) + 1
        found = _scan_one(msg, index, me)
        mentions.extend(found["mentions"])
        my_tasks.extend(found["tasks"])
        questions.extend(found["questions"])
        decisions.extend(found["decisions"])
        risks.extend(found["risks"])
    lines = [
        f"# 群消息摘要（{source}，共 {len(messages)} 条，{len(by_sender)} 人发言）",
        f"@{me} 相关 {len(mentions)} 条，其中任务候选 {len(my_tasks)} 条。",
    ]
    for task in my_tasks[:_MAX_BRIEF_ITEMS]:
        due_text = task["due"] or "未给截止"
        lines.append(f"- @我任务：{task['task']}（来自{task['from']}，{due_text}）")
    if questions:
        lines.append(f"关键问句 {len(questions)} 条，首条：{questions[0]}")
    if decisions:
        lines.append(f"决议 {len(decisions)} 条，首条：{decisions[0]}")
    if risks:
        lines.append(f"风险提示 {len(risks)} 条，首条：{risks[0]}")
    if not mentions and not questions and not decisions and not risks:
        lines.append("无@本人、无问句、无决议、无风险提示——本批消息为一般同步。")
    return {
        "source": f"input.messages:{source}",
        "total": len(messages),
        "by_sender": by_sender,
        "mentions": mentions,
        "my_tasks": my_tasks,
        "questions": questions[:_MAX_BRIEF_ITEMS],
        "decisions": decisions[:_MAX_BRIEF_ITEMS],
        "risks": risks[:_MAX_BRIEF_ITEMS],
        "brief": "\n".join(lines),
        "digested_at": _now_text(),
        "note": "摘录原文不推断；due 为 null 表示原文未给截止；定时调度待 §2.11 定时任务框架",
    }


def specs() -> tuple[ToolSpec, ...]:
    """群消息摘要工具的 ToolSpec（读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.im.digest",
            scope=SCOPE_READ,
            description="群消息摘要（入参驱动，不接真实 IM）：总数/分人计数 + @本人条目摘录 + "
            "@本人任务候选（动作词+截止正则，提不出置 null）+ 问句/决议/风险三类摘录 + 中文简报",
            params={
                "type": "object",
                "properties": {
                    "messages": {
                        "type": "array",
                        "description": "消息数组（1-100 条），元素 {sender, text, time?}",
                        "items": {"type": "object"},
                    },
                    "me": {
                        "type": "string",
                        "description": "本人在群里的称呼（用于识别 @）",
                        "minLength": 1,
                    },
                    "source": {
                        "type": "string",
                        "description": "群来源（feishu/wechat/generic，仅标注）",
                        "enum": ["feishu", "wechat", "generic"],
                    },
                },
                "required": ["messages", "me"],
                "additionalProperties": False,
            },
            handler=_im_digest,
        ),
    )


def register_all() -> list[str]:
    """注册群消息摘要工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
