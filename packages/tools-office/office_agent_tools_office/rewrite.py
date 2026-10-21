"""文案润色改写 + 整句多语种翻译（office.text.rewrite / office.text.translate，PRD §2.1 / §5.4）。

职责：
- office.text.rewrite（office:read，免审批——只产文本无落盘副作用）：把一段话按指定风格
  润色改写（正式 / 简洁 / 口语 / 专业 / 更有说服力），**不改变事实与数字**；
- office.text.translate（office:read，免审批）：整句多语种翻译（术语统一交给
  ``office.terms.translate`` 的确定性词表，本工具只做句��翻译）。

降级口径（降级绝不 500）：LLM_PROVIDERS 未配置 / 出站失败 / 返回空文本 →
      **原文直出**（degraded=True + degrade_reason），绝不编造译文或改写结果。
      出站实现与 office.doc.compose 共用 llm_text（同一行为只允许一个实现）。

数字红线：润色/翻译只允许搬运原文里的数字，改写后新增的数字记入失信清单（只标注不拦截）。
分层红线：tools-office 不 import runtime（runtime→tools 单向），故自读 os.environ 的
      LLM_PROVIDERS（与 runtime planner/llm.py 同键同构），凭据只走环境变量绝不入库。
对齐：AGENTS.md §3（降级置 degraded 标记/绝不 500）；智能办公Agent 产品需求文档.md §2.1、§5.4。
"""

from __future__ import annotations

import logging
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from .llm_text import LlmTextError, chat_text, check_numbers, now_text, provider_of

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

#: 文本长度上限（字符）：超出即拒收（防顶爆小模型上下文，也防拿它当搬运工）
_MAX_CHARS = 8000

#: 润色风格档位（值即给模型的指令口径，未知档位拒收而非放任）
_REWRITE_STYLES: dict[str, str] = {
    "formal": "改写得更正式、书面、适合对外公文或工作邮件",
    "concise": "改写得更简洁，去掉冗余表达但保留全部信息",
    "plain": "改写得更口语化、易读，适合内部沟通",
    "professional": "改写得更专业，术语准确、逻辑清晰",
    "persuasive": "改写得更有说服力，突出结论与依据但不夸大",
    "shorter": "压缩到原文一半以内，保留要点",
}

#: 目标语言白名单（中文名 / 常见代码 → 统一英文口径给模型）
_TARGET_LANGS: dict[str, str] = {
    "英语": "English",
    "英文": "English",
    "en": "English",
    "en-us": "English",
    "日语": "Japanese",
    "日文": "Japanese",
    "ja": "Japanese",
    "韩语": "Korean",
    "ko": "Korean",
    "法语": "French",
    "fr": "French",
    "德语": "German",
    "de": "German",
    "西班牙语": "Spanish",
    "es": "Spanish",
    "俄语": "Russian",
    "ru": "Russian",
    "繁体中文": "Traditional Chinese",
    "zh-tw": "Traditional Chinese",
    "越南语": "Vietnamese",
    "vi": "Vietnamese",
}


def _text_or_raise(args: dict[str, Any], field: str = "text") -> str:
    """文本入参校验：非空、长度上限（坏值给中文可操作提示）。"""
    text = str(args.get(field) or "").strip()
    if not text:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 {field} 不能为空")
    if len(text) > _MAX_CHARS:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"参数 {field} 长度不能超过 {_MAX_CHARS} 字符（当前 {len(text)}）",
        )
    return text


def _profile_of(args: dict[str, Any]) -> str:
    """LLM profile 名（缺省 default）。"""
    return str(args.get("profile") or "default").strip() or "default"


def _style_of(args: dict[str, Any]) -> tuple[str, str]:
    """润色风格（键 → 中文口径）；未知档位拒收。"""
    key = str(args.get("style") or "formal").strip().lower() or "formal"
    if key not in _REWRITE_STYLES:
        known = "、".join(sorted(_REWRITE_STYLES))
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 style 取值只能是：{known}")
    return key, _REWRITE_STYLES[key]


def _target_lang_of(args: dict[str, Any]) -> tuple[str, str]:
    """目标语言（入参 → 统一英文口径）；白名单外拒收（不猜语言）。"""
    raw = str(args.get("target_lang") or "").strip()
    if not raw:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, "参数 target_lang 不能为空：请指定目标语言（如 en / 日语）"
        )
    key = raw.lower()
    if key not in _TARGET_LANGS:
        known = "、".join(sorted(set(_TARGET_LANGS)))
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 target_lang「{raw}」不在白名单内（可用：{known}）"
        )
    return raw, _TARGET_LANGS[key]


async def _text_rewrite(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.text.rewrite：按风格润色改写；LLM 不可用 → 原文直出（degraded）。"""
    _ = ctx
    text = _text_or_raise(args)
    style_key, style_desc = _style_of(args)
    profile = _profile_of(args)

    composed_by = "llm"
    degraded = False
    degrade_reason = ""
    model = ""
    result = text
    try:
        cfg = provider_of(profile)
        model = cfg.model
        result = await chat_text(
            cfg,
            (
                "你是中文办公文案润色助手。保持原文事实、数字、专有名词与结论不变，"
                "只做表达层面的改写；不得新增原文没有的事实或数字；只输出改写后的正文。"
            ),
            f"润色要求：{style_desc}\n\n原文：\n{text}\n\n请输出改写后的正文。",
        )
    except LlmTextError as exc:
        logger.warning("office.text.rewrite 降级原文直出：%s", str(exc)[:200])
        composed_by = "fallback"
        degraded = True
        degrade_reason = str(exc)[:200]
        result = text

    return {
        "text": result,
        "style": style_key,
        "source_chars": len(text),
        "composed_by": composed_by,
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "model": model,
        "numbers_check": check_numbers(result, text),
        "generated_at": now_text(),
    }


async def _text_translate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.text.translate：整句多语种翻译；LLM 不可用 → 原文直出（degraded）。"""
    _ = ctx
    text = _text_or_raise(args)
    raw_lang, lang = _target_lang_of(args)
    profile = _profile_of(args)

    composed_by = "llm"
    degraded = False
    degrade_reason = ""
    model = ""
    result = text
    try:
        cfg = provider_of(profile)
        model = cfg.model
        result = await chat_text(
            cfg,
            (
                "你是专业文档翻译。忠实原意，不增删信息，数字与专有名词保持原样；"
                "只输出译文正文，不要解释。"
            ),
            f"目标语言：{lang}\n\n原文：\n{text}\n\n请输出译文。",
        )
    except LlmTextError as exc:
        logger.warning("office.text.translate 降级原文直出：%s", str(exc)[:200])
        composed_by = "fallback"
        degraded = True
        degrade_reason = str(exc)[:200]
        result = text

    return {
        "text": result,
        "target_lang": raw_lang,
        "target_language": lang,
        "source_chars": len(text),
        "composed_by": composed_by,
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "model": model,
        "numbers_check": check_numbers(result, text),
        "generated_at": now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """润色/翻译两工具的 ToolSpec（读口径免审批：只产文本无落盘副作用）。

    timeout_seconds=90：LLM 出站默认 60s，executor 默认 30s 会掐死；max_retries=0：
    工具内部已降级原文直出，外层重试只会把延迟放大 N 倍。
    """
    return (
        ToolSpec(
            name="office.text.rewrite",
            scope=SCOPE_READ,
            timeout_seconds=90.0,
            max_retries=0,
            description=(
                "文案润色改写（读口径，免审批）：按指定风格把原文改写得更正式/简洁/口语/专业/有说服力，"
                "保持事实与数字不变；大模型不可用时降级原文直出（degraded 标记），绝不编造改写结果"
            ),
            params={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "待润色的原文（唯一事实来源）",
                        "minLength": 1,
                        "maxLength": _MAX_CHARS,
                    },
                    "style": {
                        "type": "string",
                        "description": "润色风格（缺省 formal）",
                        "enum": sorted(_REWRITE_STYLES),
                    },
                    "profile": {
                        "type": "string",
                        "description": "LLM profile 名（可选，默认 default）",
                        "maxLength": 64,
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=_text_rewrite,
        ),
        ToolSpec(
            name="office.text.translate",
            scope=SCOPE_READ,
            timeout_seconds=90.0,
            max_retries=0,
            description=(
                "整句多语种翻译（读口径，免审批）：把原文译成指定语言（英/日/韩/法/德/西/俄/繁中/越等），"
                "忠实原意不改数字；大模型不可用时降级原文直出（degraded 标记），绝不编造译文"
            ),
            params={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "待翻译原文（唯一事实来源）",
                        "minLength": 1,
                        "maxLength": _MAX_CHARS,
                    },
                    "target_lang": {
                        "type": "string",
                        "description": "目标语言（如 en / 日语 / 繁体中文）",
                        "minLength": 1,
                        "maxLength": 32,
                    },
                    "profile": {
                        "type": "string",
                        "description": "LLM profile 名（可选，默认 default）",
                        "maxLength": 64,
                    },
                },
                "required": ["text", "target_lang"],
                "additionalProperties": False,
            },
            handler=_text_translate,
        ),
    )


def register_all() -> list[str]:
    """注册润色/翻译工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
