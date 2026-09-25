"""邮件智能处理工具（PRD §2.7：归类 / 回复草稿 / 行动项提取 / 对外预审）。

职责（四工具全读口径，入参驱动——不接真实邮箱，杜绝假数据源）：
- office.mail.classify：邮件批量归类（spam 垃圾营销 / action 需办理 / reply 需回复 /
  info 知会备案），关键词命中口径确定性摘录，未命中归 informational 不推断；
- office.mail.reply_draft：回复草稿模板直出（称呼/正文/收尾），要点原样进正文，
  缺要点留【请补充】占位并在 placeholders 如实列出，绝不代编内容；
- office.mail.action_items：正文行动项提取（动作句 + 责任人 @/由X/ X负责 + 截止
  日期/中文时间正则），提不出的字段置 null 不臆造；转待办由调用方走
  office.todo.create（写动作恒送审），本工具只出清单不落库；
- office.mail.precheck：对外邮件预审——复用 compliance 三类规则（隐私/绝对化用语/
  凭据泄密）+ 命令催促语气词表，任一命中 need_confirm=True（发送前二次确认）。

链路：__init__.register_all() → registry → executor；precheck 经 .compliance 的
扫描原语（同包复用零复制，budget→data_analysis 同口径先例）。
红线：全读免审；只摘录原文命中片段不推断；出参带 source + scanned_at 溯源。
后置说明（如实）：群消息定时摘要需 IM 消息源经 linkage 白名单工具接入（本仓无
消息源，接了才有真数据可摘要）；自动发信属对外写通道，恒送审红线外还需邮箱系统
对接，均不在本模块。
对齐：AGENTS.md §3（降级不 500/溯源/不编造）；智能办公Agent 产品需求文档.md §2.7。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from . import compliance

SCOPE_READ = "office:read"

_MAX_EMAILS = 20
_MAX_BODY = 20_000

#: 归类词表（确定性关键词命中；优先级 spam > action > reply > info，未命中 informational）
_CLASSIFY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("spam", ("优惠", "促销", "中奖", "点击链接", "代开发票", "免费试用", "限时抢购")),
    ("action", ("请处理", "请审批", "请确认", "请尽快", "请于", "麻烦", "需要你", "办理")),
    ("reply", ("盼复", "请问", "是否", "能否", "方便吗", "？", "?")),
    ("info", ("抄送", "知会", "备案", "周知", "特此通知")),
)
_CATEGORY_LABELS = {
    "spam": "垃圾/营销",
    "action": "需办理",
    "reply": "需回复",
    "info": "知会备案",
    "informational": "一般告知",
}

#: 行动项口径：动作词 + 责任人（@X / 由X / X负责）+ 截止（ISO 日期 / 中文时间）
_ACTION_VERBS = ("完成", "提交", "提供", "审核", "回复", "确认", "发送", "整理", "处理", "定稿")
_RE_OWNER = re.compile(
    r"@([\u4e00-\u9fa5A-Za-z]{2,10})|由\s*([\u4e00-\u9fa5A-Za-z]{2,10})|([\u4e00-\u9fa5]{2,10})负责"
)
_RE_DUE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日|本周[一二三四五六日]|下周[一二三四五六日]"
    r"|今天|明天|后天|大后天|月底"
)

#: 语气风险词表（命令/催促向，对外邮件易激化——预审只摘录命中词，是否发送交人判断）
_TONE_WORDS = ("必须", "立刻", "马上", "怎么还没", "搞快点", "否则", "责任自负", "最后提醒")


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _emails_or_raise(raw: Any) -> list[dict[str, Any]]:
    """邮件入参口径：1-20 封对象数组，subject/body 至少一项非空。"""
    if not isinstance(raw, list) or not raw:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            '参数 emails 必须是非空数组，元素形如 {"subject": "主题", "body": "正文"}',
        )
    if len(raw) > _MAX_EMAILS:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 emails 最多 {_MAX_EMAILS} 封")
    emails: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise BusinessError(ErrorCode.PARAM_INVALID, f"第 {index} 封必须是对象（subject/body）")
        subject = str(item.get("subject") or "").strip()
        body = str(item.get("body") or "").strip()
        if not subject and not body:
            raise BusinessError(ErrorCode.PARAM_INVALID, f"第 {index} 封 subject/body 至少一项非空")
        if len(body) > _MAX_BODY:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"第 {index} 封正文最长 {_MAX_BODY} 字符（当前 {len(body)}）",
            )
        emails.append({"subject": subject, "body": body})
    return emails


def _classify_one(subject: str, body: str) -> tuple[str, list[str]]:
    """单封归类：按规则优先级取首个命中类别，返回 (category, 命中关键词)。"""
    text = f"{subject}\n{body}"
    for category, words in _CLASSIFY_RULES:
        hits = [word for word in words if word in text]
        if hits:
            return category, hits
    return "informational", []


async def _mail_classify(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.mail.classify：批量邮件规则归类（读，免审）。"""
    _ = ctx
    emails = _emails_or_raise(args.get("emails"))
    results = []
    for index, mail in enumerate(emails, 1):
        category, keywords = _classify_one(mail["subject"], mail["body"])
        results.append(
            {
                "index": index,
                "subject": mail["subject"],
                "category": category,
                "category_label": _CATEGORY_LABELS[category],
                "matched_keywords": keywords,
            }
        )
    counts: dict[str, int] = {}
    for item in results:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    return {
        "results": results,
        "counts": counts,
        "source": "input.emails",
        "scanned_at": _now_text(),
        "note": "关键词命中口径归类，未命中归一般告知不推断",
    }


async def _mail_reply_draft(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.mail.reply_draft：回复草稿模板直出，缺要点留占位不代编。"""
    subject = str(args.get("subject") or "").strip()
    if not subject:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 subject 不能为空：请传入被回复邮件主题")
    sender_name = str(args.get("sender_name") or "").strip()
    tone = str(args.get("tone") or "formal").strip()
    if tone not in ("formal", "friendly"):
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 tone 只能是 formal/friendly（当前：{tone}）"
        )
    points_raw = args.get("key_points") or []
    if not isinstance(points_raw, list):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 key_points 必须是字符串数组")
    points = [str(p).strip() for p in points_raw if str(p).strip()]
    placeholders: list[str] = []
    greeting = (
        f"{sender_name}，{'您好' if tone == 'formal' else '你好'}：" if sender_name else "您好："
    )
    if not sender_name:
        placeholders.append("称呼")
    opening = (
        "感谢来信，现就您提出的问题回复如下。" if tone == "formal" else "收到你的消息，回复如下。"
    )
    if points:
        body_lines = [f"{i}. {p}" for i, p in enumerate(points, 1)]
    else:
        body_lines = ["【请补充正文要点】"]
        placeholders.append("正文要点")
    closing = "如有其他问题请随时联系。" if tone == "formal" else "有问题随时找我。"
    signature = "【请补充署名】"
    placeholders.append("署名")
    draft = "\n".join([greeting, "", opening, "", *body_lines, "", closing, "", signature])
    _ = ctx
    return {
        "reply_subject": f"Re: {subject}",
        "tone": tone,
        "draft": draft,
        "placeholders": placeholders,
        "source": "input.key_points",
        "note": "模板直出草稿，占位字段请补充后再发送（对外发送前建议过 office.mail.precheck）",
    }


def _extract_owner(sentence: str) -> str | None:
    """责任人提取：@X / 由X / X负责 三形态，取首个命中组。"""
    match = _RE_OWNER.search(sentence)
    if not match:
        return None
    return next((group for group in match.groups() if group), None)


async def _mail_action_items(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.mail.action_items：正文行动项提取（动作句+责任人+截止，提不出置 null）。"""
    _ = ctx
    body = str(args.get("body") or "").strip()
    if not body:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 body 不能为空：请传入邮件正文")
    if len(body) > _MAX_BODY:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 body 最长 {_MAX_BODY} 字符")
    items: list[dict[str, Any]] = []
    for sentence in re.split(r"[。！！;\n；]+", body):
        text = sentence.strip(" ，,：:")
        if not text or not any(verb in text for verb in _ACTION_VERBS):
            continue
        due_match = _RE_DUE.search(text)
        items.append(
            {
                "action": text[:120],
                "owner": _extract_owner(text),
                "due": due_match.group(0) if due_match else None,
            }
        )
    return {
        "items": items,
        "count": len(items),
        "source": "input.body",
        "extracted_at": _now_text(),
        "note": "owner/due 为 null 表示原文未给出，不臆造；转待办请走 office.todo.create（写动作恒送审）",
    }


async def _mail_precheck(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.mail.precheck：对外邮件预审（compliance 三类规则 + 语气风险词表）。"""
    _ = ctx
    text = str(args.get("text") or "")
    if not text.strip():
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 text 不能为空：请传入拟外发邮件正文")
    if len(text) > _MAX_BODY:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 text 最长 {_MAX_BODY} 字符")
    claimed: list[tuple[int, int]] = []
    privacy, _ = compliance._scan_privacy(text, claimed)
    hits = compliance._sort_hits(
        privacy + compliance._scan_words(text) + compliance._scan_secrets(text)
    )
    tone_hits = [
        {"category": "tone", "rule": "命令/催促语气", "excerpt": word, "position": text.find(word)}
        for word in _TONE_WORDS
        if word in text
    ]
    hits = compliance._sort_hits(hits + tone_hits)
    passed = not hits
    tips: list[str] = []
    if any(hit["category"] == "tone" for hit in hits):
        tips.append("命中命令/催促语气：对外邮件建议改用协商表述，避免激化")
    if not passed:
        tips.append("预审未通过：请处理后重新预审，确认无风险再发送")
    return {
        "passed": passed,
        "need_confirm": not passed,
        "hit_count": len(hits),
        "hits": hits[: compliance._MAX_HITS],
        "truncated": len(hits) > compliance._MAX_HITS,
        "tips": tips,
        "source": "input.text",
        "scanned_at": _now_text(),
        "note": "预审只摘录命中片段不推断；隐私/凭据命中处理口径同 office.compliance.scan",
    }


def specs() -> tuple[ToolSpec, ...]:
    """邮件四工具 ToolSpec（全读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.mail.classify",
            scope=SCOPE_READ,
            description="邮件批量归类（入参驱动，不接真实邮箱）：垃圾营销/需办理/需回复/知会备案四类关键词命中口径，未命中归一般告知不推断",
            params={
                "type": "object",
                "properties": {
                    "emails": {
                        "type": "array",
                        "description": f"邮件数组（1-{_MAX_EMAILS} 封），元素 {{subject, body}}",
                        "items": {"type": "object"},
                    }
                },
                "required": ["emails"],
                "additionalProperties": False,
            },
            handler=_mail_classify,
        ),
        ToolSpec(
            name="office.mail.reply_draft",
            scope=SCOPE_READ,
            description="邮件回复草稿模板直出：称呼/正文要点/收尾按语气（formal/friendly）成稿，缺项留占位并列出 placeholders，绝不代编",
            params={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "被回复邮件主题", "minLength": 1},
                    "sender_name": {"type": "string", "description": "对方称呼（缺省留占位）"},
                    "tone": {
                        "type": "string",
                        "description": "语气（formal 正式 / friendly 亲切，默认 formal）",
                        "enum": ["formal", "friendly"],
                    },
                    "key_points": {
                        "type": "array",
                        "description": "回复要点（原样进正文）",
                        "items": {"type": "string"},
                    },
                },
                "required": ["subject"],
                "additionalProperties": False,
            },
            handler=_mail_reply_draft,
        ),
        ToolSpec(
            name="office.mail.action_items",
            scope=SCOPE_READ,
            description="邮件正文行动项提取：动作句 + 责任人（@X/由X/X负责）+ 截止日期（ISO/中文时间）正则摘录，提不出置 null 不臆造；转待办走 office.todo.create",
            params={
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "邮件正文", "minLength": 1},
                },
                "required": ["body"],
                "additionalProperties": False,
            },
            handler=_mail_action_items,
        ),
        ToolSpec(
            name="office.mail.precheck",
            scope=SCOPE_READ,
            description="对外邮件预审：隐私/绝对化用语/凭据泄密（复用 compliance 规则）+ 命令催促语气词表，命中即 need_confirm 二次确认提示",
            params={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "拟外发的邮件正文", "minLength": 1},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=_mail_precheck,
        ),
    )


def register_all() -> list[str]:
    """注册邮件四工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
