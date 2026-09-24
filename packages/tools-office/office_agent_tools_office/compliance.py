"""合规风险检测工具（office.compliance.scan，对齐 PRD §2.7/§4.1/§5.3 V1.2「合规风险检测」）。

职责：
- office.compliance.scan（office:read）：对文本做确定性规则扫描——
  ①隐私信息（手机号/身份证号/银行卡号正则命中）；②违规用语（绝对化用语词表命中）；
  ③疑似泄密凭据（password/token/api key 等键值对命中，摘录时脱敏）。
  只摘录命中片段与位置，不推断、不改写原文；无命中 passed=True 不降级。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：读口径（免审批）；纯本地实现，不触及 ORM / FastAPI；命中摘录自原文
      （凭据值脱敏防二次泄露），全部命中数超过上限时如实标 truncated。
对齐：AGENTS.md §3（分层红线：工具实现纯函数）；智能办公Agent 产品需求文档.md
      §2.7（对外邮件预审：敏感词、泄密检查）、§4.1（内置敏感词与合规检测）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

SCOPE_READ = "office:read"

_MAX_TEXT = 20_000
_MAX_HITS = 50

# 命中片段摘录上限（只截原文，不改写）
_EXCERPT_MAX = 40

#: 隐私信息正则（先长后短：身份证优先登记占位，避免被银行卡规则重复计数）
_RE_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_RE_BANK_CARD = re.compile(r"(?<!\d)\d{15,19}(?!\d)")
_RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

#: 疑似泄密凭据（键值对形态；值在摘录中脱敏，防扫描结果二次泄露）
_RE_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|token|access[_-]?key)\s*[=:：]\s*(\S{4,})"
)

#: 违规用语词表（绝对化用语，确定性扫描；命中即摘录原文片段）
_ABSOLUTE_WORDS = (
    "全网最低",
    "最低价",
    "史上最",
    "第一品牌",
    "百分百",
    "绝对有效",
    "稳赚不赔",
    "包治百病",
)

_SCENE_LABELS: dict[str, str] = {
    "email": "对外邮件",
    "notice": "通知公告",
    "document": "文档",
}


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _excerpt(text: str, start: int, end: int) -> str:
    """摘录原文命中片段（超长截断加省略号，绝不改写命中内容）。"""
    fragment = text[start:end]
    if len(fragment) > _EXCERPT_MAX:
        return fragment[:_EXCERPT_MAX] + "…"
    return fragment


def _overlap(start: int, end: int, claimed: list[tuple[int, int]]) -> bool:
    """该区间是否已被更早规则登记（身份证优先，防同一串数字重复报两类）。"""
    return any(start < c_end and c_start < end for c_start, c_end in claimed)


def _scan_privacy(
    text: str, claimed: list[tuple[int, int]]
) -> tuple[list[dict[str, Any]], list[tuple[int, int]]]:
    """隐私信息扫描：身份证 → 银行卡（跳过已登记区间）→ 手机号，命中区间回填 claimed。"""
    hits: list[dict[str, Any]] = []
    for match in _RE_ID_CARD.finditer(text):
        claimed.append(match.span())
        hits.append(
            {
                "category": "privacy",
                "rule": "身份证号",
                "excerpt": _excerpt(text, *match.span()),
                "position": match.start(),
            }
        )
    for match in _RE_BANK_CARD.finditer(text):
        if _overlap(match.start(), match.end(), claimed):
            continue
        claimed.append(match.span())
        hits.append(
            {
                "category": "privacy",
                "rule": "银行卡号",
                "excerpt": _excerpt(text, *match.span()),
                "position": match.start(),
            }
        )
    for match in _RE_PHONE.finditer(text):
        if _overlap(match.start(), match.end(), claimed):
            continue
        claimed.append(match.span())
        hits.append(
            {
                "category": "privacy",
                "rule": "手机号",
                "excerpt": _excerpt(text, *match.span()),
                "position": match.start(),
            }
        )
    return hits, claimed


def _mask_secret(value: str) -> str:
    """凭据值脱敏：只留前 2 字符 + **（防扫描结果本身成为泄密源）。"""
    return value[:2] + "**"


def _scan_secrets(text: str) -> list[dict[str, Any]]:
    """疑似泄密凭据扫描：键值对命中，摘录形如 password=ab**（值脱敏）。"""
    hits: list[dict[str, Any]] = []
    for match in _RE_SECRET.finditer(text):
        key, value = match.group(1), match.group(2)
        snippet = f"{key}={_mask_secret(value)}"
        hits.append(
            {
                "category": "leak",
                "rule": "疑似凭据明文",
                "excerpt": snippet,
                "position": match.start(),
            }
        )
    return hits


def _scan_words(text: str) -> list[dict[str, Any]]:
    """违规用语扫描：绝对化用语词表逐词查找（确定性，无 NLP 推断）。"""
    hits: list[dict[str, Any]] = []
    for word in _ABSOLUTE_WORDS:
        start = 0
        while (found := text.find(word, start)) != -1:
            end = found + len(word)
            hits.append(
                {
                    "category": "words",
                    "rule": "绝对化用语",
                    "excerpt": _excerpt(text, found, end),
                    "position": found,
                }
            )
            start = end
    return hits


def _sort_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按出现位置排序（阅读顺序 = 原文顺序）。"""
    return sorted(hits, key=lambda hit: hit["position"])


async def _compliance_scan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.compliance.scan：文本合规扫描（隐私/违规用语/泄密凭据三类规则）。"""
    _ = ctx
    text = str(args.get("text") or "")
    if not text.strip():
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 text 不能为空：请传入待检测文本")
    if len(text) > _MAX_TEXT:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 text 最长 {_MAX_TEXT} 字符（当前 {len(text)} 字符）"
        )
    scene = str(args.get("scene") or "document").strip()
    if scene not in _SCENE_LABELS:
        valid = "、".join(sorted(_SCENE_LABELS))
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 scene 只能是 {valid}（当前：{scene}）")

    claimed: list[tuple[int, int]] = []
    privacy, claimed = _scan_privacy(text, claimed)
    hits = _sort_hits(privacy + _scan_words(text) + _scan_secrets(text))
    truncated = len(hits) > _MAX_HITS
    hits = hits[:_MAX_HITS]

    counts = {
        "privacy": sum(1 for hit in hits if hit["category"] == "privacy"),
        "words": sum(1 for hit in hits if hit["category"] == "words"),
        "leak": sum(1 for hit in hits if hit["category"] == "leak"),
    }
    tips: list[str] = []
    if counts["privacy"]:
        tips.append("命中隐私信息：外发前请脱敏（打码/删除），避免个人信息泄露")
    if counts["words"]:
        tips.append("命中绝对化用语：对外物料请按广告法与公司口径改写")
    if counts["leak"]:
        tips.append("命中疑似凭据明文：请立即移除并轮换该凭据")
    if scene == "email" and hits:
        tips.append("对外邮件预审未通过：请处理后重新扫描再发送")
    return {
        "scene": scene,
        "scene_label": _SCENE_LABELS[scene],
        "passed": not hits,
        "hit_count": len(hits),
        "counts": counts,
        "hits": hits,
        "truncated": truncated,
        "tips": tips,
        "source": "input.text",
        "scanned_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """合规扫描工具的 ToolSpec（读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.compliance.scan",
            scope=SCOPE_READ,
            description="文本合规风险扫描：隐私信息（手机号/身份证/银行卡）、绝对化用语、"
            "疑似泄密凭据三类确定性规则；只摘录命中片段与位置不推断（凭据值脱敏），"
            "适用于对外邮件预审、通知公告与文档外发前自查",
            params={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": f"待检测文本（1-{_MAX_TEXT} 字符）",
                        "minLength": 1,
                        "maxLength": _MAX_TEXT,
                    },
                    "scene": {
                        "type": "string",
                        "description": "使用场景（email 对外邮件 / notice 通知公告 / document 文档，缺省 document）",
                        "enum": ["email", "notice", "document"],
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=_compliance_scan,
        ),
    )


def register_all() -> list[str]:
    """注册合规扫描工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
