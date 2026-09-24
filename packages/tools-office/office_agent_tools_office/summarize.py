"""文本处理工具（office.text.summarize / normalize，对齐 PRD §2.1 文档处理）。

职责：
- office.text.summarize（office:read）：抽取式摘要——按字符 bigram 词频给句子打分，
  取 top N 原句按原文顺序拼接 + 要点列表；多篇模式（texts 数组）逐篇一句话 + 联合摘要；
  摘要全部是原文原句，不改写不编造；
- office.text.normalize（office:read）：格式统一——行尾空白清除 + 空行压缩 + 全角空格
  归一 + 首尾去空，返回改动计数；只做空白与换行层面的确定性归一，不改一字正文。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：纯本地实现，不触及 ORM / FastAPI；摘要是原文摘录（无 LLM 不做抽象改写）；
      文案润色改写需大模型，明确后置（见模块末尾说明），绝不用模板假装润色。
对齐：AGENTS.md §3（分层/文本不编造类推数值不编造）；智能办公Agent 产品需求文档.md
      §2.1（文档处理：长文摘要/重点提炼/多文档整合总结/格式统一调整）。
"""

from __future__ import annotations

import re
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

SCOPE_READ = "office:read"

_MAX_TEXT_CHARS = 20000
_MAX_DOCS = 10
_MAX_SENTENCES = 10

#: 句子切分：中英文句末标点 + 换行（保留标点，摘录不断章）
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?])|\n+")


def _sentences(text: str) -> list[str]:
    """切句：按句末标点/换行切分，去空去超长（单句超 500 字截断防刷屏）。"""
    parts = [part.strip() for part in _SENT_SPLIT_RE.split(text) if part.strip()]
    return [part[:500] for part in parts if part]


def _bigrams(text: str) -> list[str]:
    """字符 bigram 序列（含标点，不过滤——词频即权重，简单可复现）。"""
    cleaned = "".join(ch for ch in text if not ch.isspace())
    return [cleaned[i : i + 2] for i in range(len(cleaned) - 1)]


def _extractive(text: str, max_sentences: int) -> tuple[list[str], int]:
    """抽取式摘要：bigram 词频打分 → 取 top N → 按原文顺序返回（附原文总句数）。"""
    sents = _sentences(text)
    if not sents:
        return [], 0
    freq: dict[str, int] = {}
    for gram in _bigrams(text):
        freq[gram] = freq.get(gram, 0) + 1
    scored = sorted(
        range(len(sents)),
        key=lambda i: (
            -sum(freq.get(gram, 0) for gram in _bigrams(sents[i])),
            i,
        ),
    )
    picked = sorted(scored[:max_sentences])
    return [sents[i] for i in picked], len(sents)


def _text_or_raise(value: Any, name: str, max_chars: int) -> str:
    """单文本口径：非空字符串 + 长度上限（超长请分篇走 texts 多篇模式）。"""
    text = str(value or "").strip() if isinstance(value, str) else ""
    if not text:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 {name} 不能为空：请传入待处理文本")
    if len(text) > max_chars:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 {name} 最长 {max_chars} 字（当前 {len(text)}）"
        )
    return text


async def _text_summarize(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.text.summarize：单篇抽取摘要 / 多篇逐篇一句 + 联合摘要。"""
    _ = ctx
    max_sentences = args.get("max_sentences", 3)
    if not isinstance(max_sentences, int) or isinstance(max_sentences, bool):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 max_sentences 必须是整数")
    if not 1 <= max_sentences <= _MAX_SENTENCES:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 max_sentences 必须是 1-{_MAX_SENTENCES} 的整数"
        )
    texts = args.get("texts")
    if texts is not None:
        if not isinstance(texts, list) or not texts:
            raise BusinessError(ErrorCode.PARAM_INVALID, "参数 texts 必须是非空数组")
        if len(texts) > _MAX_DOCS:
            raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 texts 最多 {_MAX_DOCS} 篇")
        per_doc: list[dict[str, Any]] = []
        for index, item in enumerate(texts):
            doc = _text_or_raise(item, f"texts[{index}]", _MAX_TEXT_CHARS // 4)
            points, total = _extractive(doc, 1)
            per_doc.append(
                {"index": index, "one_liner": points[0] if points else "", "sentences": total}
            )
        joint = "；".join(item["one_liner"] for item in per_doc if item["one_liner"])
        return {
            "mode": "multi",
            "doc_count": len(per_doc),
            "per_doc": per_doc,
            "joint_summary": joint,
            "method": "抽取式：每篇取词频最高 1 句原句拼接，未改写",
            "source": "input.texts",
        }
    text = _text_or_raise(args.get("text"), "text", _MAX_TEXT_CHARS)
    points, total = _extractive(text, max_sentences)
    return {
        "mode": "single",
        "summary": "".join(points),
        "key_points": points,
        "picked": len(points),
        "total_sentences": total,
        "method": "抽取式：bigram 词频打分取 top 原句，未改写",
        "source": "input.text",
    }


async def _text_normalize(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.text.normalize：空白与换行归一（不动正文一字）。"""
    _ = ctx
    text = _text_or_raise(args.get("text"), "text", _MAX_TEXT_CHARS)
    lines = text.replace("\u3000", " ").split("\n")
    trailing_fixed = sum(1 for line in lines if line != line.rstrip())
    lines = [line.rstrip() for line in lines]
    collapsed = "\n".join(lines)
    blank_removed = len(re.findall(r"\n{3,}", collapsed))
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed).strip()
    return {
        "normalized": collapsed,
        "changes": {
            "trailing_spaces_fixed": trailing_fixed,
            "blank_blocks_collapsed": blank_removed,
        },
        "note": "仅归一空白与换行（行尾空格/连续空行/全角空格/首尾空行），正文一字未改",
        "source": "input.text",
    }


def specs() -> tuple[ToolSpec, ...]:
    """两个文本工具的 ToolSpec（读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.text.summarize",
            scope=SCOPE_READ,
            description="抽取式文本摘要：单篇取词频最高原句拼接 + 要点列表，或多篇逐篇一句话 + 联合摘要；摘要全是原文原句，不改写不编造",
            params={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "待摘要单篇文本（与 texts 二选一）"},
                    "texts": {
                        "type": "array",
                        "description": "多篇文本数组（与 text 二选一，最多 10 篇）",
                        "items": {"type": "string"},
                    },
                    "max_sentences": {
                        "type": "integer",
                        "description": "单篇最多取句数（1-10，默认 3）",
                    },
                },
                "additionalProperties": False,
            },
            handler=_text_summarize,
        ),
        ToolSpec(
            name="office.text.normalize",
            scope=SCOPE_READ,
            description="文本格式统一：行尾空白清除 + 空行压缩 + 全角空格归一 + 首尾去空，只动空白换行，正文一字不改并返回改动计数",
            params={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "待归一文本", "minLength": 1},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=_text_normalize,
        ),
    )


def register_all() -> list[str]:
    """注册两个文本工具；返回已注册工具名列表。

    后置说明：文案润色改写需大模型抽象改写能力，无 LLM 时任何模板式“润色”都是
    编造（与数值不编造同级红线），故明确后置——待 LLM 规划器接入后再以
    office.text.polish（读）落地，本文件不预留假实现。
    """
    return [register(spec).name for spec in specs()]
