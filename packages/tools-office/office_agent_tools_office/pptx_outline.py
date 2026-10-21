"""PPT 大纲生成（office.pptx.outline，对齐 PRD §2.12「基于文档、周报一键生成 PPT 大纲与每页文案」）。

职责：
- office.pptx.outline（office:read，免审批——只产结构不落盘）：把一段文档/周报文本
  （或 DOCS_DIR 内文档）整理成演示大纲（每页标题 + 要点），**结构与
  ``office.pptx.generate`` 的 slides 入参同形**——大纲可直接喂给生成工具出 .pptx。

降级口径（降级绝不 500）：LLM 未配置 / 出站失败 / 返回不可解析 → 规则分段
      （按 markdown 标题行与段落切页），仍产出可用大纲并标 degraded=True，绝不编造内容。

数字红线：只搬运原文里的数字；模型引入的新数字记入失信清单（只标注不拦截）。
分层红线：文本提取复用 file_read.extract_document（同一份提取口径，不重复实现）；
      出站复用 llm_text（与 doc.compose / rewrite 同一实现）。
对齐：AGENTS.md §3（降级置 degraded 标记）；智能办公Agent 产品需求文档.md §2.12。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from . import file_read
from .llm_text import LlmTextError, chat_text, check_numbers, now_text, provider_of
from .paths import resolve_under_docs

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

#: 文本长度上限（字符）：防顶爆小模型上下文
_MAX_CHARS = 8000

#: 默认页数与页数上限（与 office.pptx.generate 的 1-30 页同口径）
_DEFAULT_SLIDES = 8
_MAX_SLIDES = 30

#: 每页要点条数上限（与生成工具的 ≤20 同口径，留余量）
_MAX_BULLETS = 8

#: markdown 标题行（规则分段的切页依据）
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")

#: 模型可能回传的 JSON 代码围栏
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _slides_or_empty(raw: Any) -> list[dict[str, Any]]:
    """解析模型给出的大纲 JSON；结构不符给空列表（调用方走规则降级，不编造）。"""
    if not isinstance(raw, dict):
        return []
    items = raw.get("slides")
    if not isinstance(items, list):
        return []
    slides: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        bullets_raw = item.get("bullets")
        bullets = (
            [
                str(b).strip()
                for b in bullets_raw
                if str(b).strip()
                if isinstance(b, (str, int, float))
            ]
            if isinstance(bullets_raw, list)
            else []
        )
        if not title or not bullets:
            continue
        slides.append({"title": title[:60], "bullets": bullets[:_MAX_BULLETS]})
    return slides


def _parse_outline_json(text: str) -> list[dict[str, Any]]:
    """从模型回复里抽大纲 JSON（容忍代码围栏与前后说明文字；抽不出给空列表）。"""
    cleaned = _FENCE.sub("", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(cleaned[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    return _slides_or_empty(payload)


def _rule_outline(text: str, max_slides: int) -> list[dict[str, Any]]:
    """规则分段降级：markdown 标题行切页，其余按段落归并（要点取原句，不改写）。"""
    slides: list[dict[str, Any]] = []
    current_title = ""
    bullets: list[str] = []

    def _flush() -> None:
        nonlocal current_title, bullets
        if current_title and bullets and len(slides) < max_slides:
            slides.append({"title": current_title[:60], "bullets": bullets[:_MAX_BULLETS]})
        current_title = ""
        bullets = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = _HEADING.match(raw_line)
        if heading:
            _flush()
            current_title = heading.group(1).strip()
            continue
        if not current_title:
            # 无标题结构时，用首行作页标题，其余作要点
            current_title = line[:60]
            continue
        bullets.append(line[:200])
        if len(bullets) >= _MAX_BULLETS:
            _flush()
    _flush()
    return slides


async def _source_text(args: dict[str, Any]) -> tuple[str, str]:
    """取素材文本：直接给 text，或给 DOCS_DIR 内文件名（走 file_read 同一提取口径）。"""
    text = str(args.get("text") or "").strip()
    filename = str(args.get("filename") or "").strip()
    if not text and not filename:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            "参数 text 与 filename 至少给一个：可直接粘贴文本，或给出文档工作目录里的文件名",
        )
    if text and filename:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, "参数 text 与 filename 只能给一个（避免两个数据源打架）"
        )
    if filename:
        path = resolve_under_docs(filename, file_read.READ_SUFFIXES)
        if not path.is_file():
            raise BusinessError(
                ErrorCode.NOT_FOUND, f"文件不存在：{path.name}（请先放入文档工作目录）", 404
            )
        if path.stat().st_size > file_read.MAX_FILE_BYTES:
            raise BusinessError(ErrorCode.PARAM_INVALID, "文件过大（超 200KB），请拆分后再生成大纲")
        result = await asyncio.to_thread(file_read.extract_document, path)
        text = str(result.get("text") or "").strip()
        if not text:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"文件没有可用文本：{path.name}（{result.get('degraded_reason') or '无文字层'}）",
            )
        return text, path.name
    if len(text) > _MAX_CHARS:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 text 长度不能超过 {_MAX_CHARS} 字符（当前 {len(text)}）"
        )
    return text, ""


async def _pptx_outline(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.pptx.outline：文档/周报 → 演示大纲（可直接喂 office.pptx.generate）。"""
    _ = ctx
    text, source_name = await _source_text(args)
    title = str(args.get("title") or "").strip() or (source_name or "演示大纲")
    max_slides = args.get("max_slides", _DEFAULT_SLIDES)
    if (
        not isinstance(max_slides, int)
        or isinstance(max_slides, bool)
        or not 1 <= max_slides <= _MAX_SLIDES
    ):
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 max_slides 必须是 1-{_MAX_SLIDES} 的整数"
        )
    profile = str(args.get("profile") or "default").strip() or "default"

    composed_by = "llm"
    degraded = False
    degrade_reason = ""
    model = ""
    slides: list[dict[str, Any]] = []
    try:
        cfg = provider_of(profile)
        model = cfg.model
        raw = await chat_text(
            cfg,
            (
                "你是演示文稿策划。把给定材料整理成 JSON 大纲，"
                '格式：{"slides":[{"title":"页标题","bullets":["要点1","要点2"]}]}；'
                "只使用材料里的事实与数字，不得编造；每页 2-6 条要点；只输出 JSON。"
            ),
            f"页数上限：{max_slides}\n标题：{title}\n\n材料：\n{text}\n\n请输出 JSON 大纲。",
        )
        slides = _parse_outline_json(raw)
        if not slides:
            raise LlmTextError("大模型未给出可解析的大纲 JSON")
    except LlmTextError as exc:
        logger.warning("office.pptx.outline 降级规则分段：%s", str(exc)[:200])
        slides = _rule_outline(text, max_slides)
        composed_by = "fallback"
        degraded = True
        degrade_reason = str(exc)[:200]

    slides = slides[:max_slides]
    if not slides:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            "材料为空或无法整理出任何页面：请补充有内容的文本或文档（不编造大纲）",
        )
    outline_text = json.dumps(slides, ensure_ascii=False)
    return {
        "title": title,
        "slides": slides,
        "slide_count": len(slides),
        "source_chars": len(text),
        "source": f"local-docs:{source_name}" if source_name else "inline-text",
        "composed_by": composed_by,
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "model": model,
        "numbers_check": check_numbers(outline_text, text),
        "next_hint": "可直接调用 office.pptx.generate（slides 入参与本工具出参同形）",
        "generated_at": now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """PPT 大纲工具的 ToolSpec（读口径免审批：只产结构，不写盘）。"""
    return (
        ToolSpec(
            name="office.pptx.outline",
            scope=SCOPE_READ,
            timeout_seconds=90.0,
            max_retries=0,
            description=(
                "PPT 大纲生成（读口径，免审批）：把文档/周报文本（或文档工作目录里的文件）"
                "整理成每页标题+要点的演示大纲，结构与 office.pptx.generate 的 slides 入参同形，"
                "可直接喂给它出 .pptx；大模型不可用时按标题行/段落规则分段降级（degraded），绝不编造"
            ),
            params={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "素材文本（与 filename 二选一）",
                        "maxLength": _MAX_CHARS,
                    },
                    "filename": {
                        "type": "string",
                        "description": "文档工作目录里的文件名（与 text 二选一）",
                        "maxLength": 120,
                    },
                    "title": {
                        "type": "string",
                        "description": "演示标题（缺省用文件名）",
                        "maxLength": 100,
                    },
                    "max_slides": {
                        "type": "integer",
                        "description": f"页数上限（1-{_MAX_SLIDES}，缺省 {_DEFAULT_SLIDES}）",
                        "minimum": 1,
                        "maximum": _MAX_SLIDES,
                    },
                    "profile": {
                        "type": "string",
                        "description": "LLM profile 名（可选，默认 default）",
                        "maxLength": 64,
                    },
                },
                "additionalProperties": False,
            },
            handler=_pptx_outline,
        ),
    )


def register_all() -> list[str]:
    """注册 PPT 大纲工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
