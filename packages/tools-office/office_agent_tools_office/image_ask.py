"""图片内容问答（office.image.ask，对齐 PRD §2.6「图片/截图 OCR 识别并问答」）。

职责：
- office.image.ask（office:read，免审）：对 DOCS_DIR 内图片（png/jpg/jpeg/bmp/gif/webp）
  先经 OCR 引擎取真实文本（复用 ocr.ocr_available 同一探针，不重复实现），再对识别文本
  按段落检索并只摘录原文片段作答；检索复用 retrieval.py 双通道（配 EMBEDDING_* 走向量，
  失败回退字符并如实标注），与 office.file.ask 同口径。
- 如实口径：引擎缺失/识别为空/零命中一律 degraded + 可操作原因，不编造半个字；
  图片元数据（格式/尺寸/色深）来自 Pillow 实测；缺文件 404、穿越 1001。
- 与 ocr.image 的分工：ocr.image 面向「整图识别出全部文本」，本工具面向
  「就图问一个具体问题」（如「这张截图里的报错码是什么」），二者共享引擎探针。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler →
      ocr.ocr_available（探针）→ pytesseract（实测文本）→ retrieval（切块+检索）。
红线：纯本地实现，不触及 ORM / FastAPI；文件名锁 DOCS_DIR；识别文本只来自引擎实测。
对齐：AGENTS.md §3（降级绝不 500 / 数据不出域）；
      智能办公Agent 产品需求文档.md §2.6（图片/截图 OCR 识别并问答）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings
from office_agent_tools_office.retrieval import (
    chunks_of,
    first_snippet,
    retrieve,
)

from . import ocr
from .paths import image_suffixes, resolve_under_docs

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _read_meta(path: Path) -> dict[str, Any]:
    """Pillow 实测图片元数据（同步 IO，调用方包 to_thread）。"""
    with Image.open(path) as img:
        return {
            "format": img.format or "",
            "width": img.width,
            "height": img.height,
            "mode": img.mode,
        }


async def _recognize(path: Path, language: str) -> tuple[str, bool, str]:
    """OCR 识别：返回 (文本, 是否可用, 不可用原因)。"""
    available, reason = await asyncio.to_thread(ocr.ocr_available)
    if not available:
        return "", False, reason
    try:
        raw = await asyncio.to_thread(ocr.pytesseract.image_to_string, str(path), lang=language)
    except Exception as exc:
        logger.warning("图片问答识别失败：%s", str(exc)[:120])
        return "", False, f"识别执行失败：{str(exc)[:100]}"
    return str(raw or "").strip(), True, ""


async def _image_ask(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.image.ask：图片 OCR 文本的段落检索问答（只摘录原文，不生成）。"""
    _ = ctx
    filename = str(args.get("file_path") or "").strip()
    query = str(args.get("query") or "").strip()
    if not query:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 query 不能为空：请输入针对图片的问题")
    top_k = args.get("top_k", 3)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 10:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 top_k 必须是 1-10 的整数")
    language = str(args.get("language") or "chi_sim+eng").strip() or "chi_sim+eng"
    if len(language) > 40:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 language 最长 40 个字符")

    path = resolve_under_docs(filename, image_suffixes())
    if not path.is_file():
        raise BusinessError(
            ErrorCode.NOT_FOUND, f"图片不存在：{path.name}（文档工作目录内未找到该文件）", 404
        )
    meta = await asyncio.to_thread(_read_meta, path)
    text, engine_ok, engine_reason = await _recognize(path, language)
    source = f"local-docs:{path.name}"
    if not engine_ok or not text:
        reason = engine_reason or "图片内没有可检索文本（识别结果为空）：请确认截图含文字且方向正常"
        return {
            "file": path.name,
            "image": meta,
            "query": query,
            "answer_fragments": [],
            "count": 0,
            "retrieval_mode": "none",
            "embedding_model": "",
            "retrieval_fallback_reason": "",
            "degraded": True,
            "degraded_reason": reason,
            "language": language,
            "source": source,
            "fetched_at": _now_text(),
        }

    chunks = chunks_of([{"title": path.name, "content": text, "source": source}], max_chars=None)
    # 三级降级链统一入口（milvus → embedding 全量重算 → bigram），口径收口在 retrieval.retrieve
    scored, mode, fallback_reason = await retrieve(query, chunks, top_k, origin="image")
    fragments = [
        {
            "snippet": first_snippet(chunk["text"], query, mode),
            "score": round(score, 4),
            "source": chunk["source"],
        }
        for score, chunk in scored
    ]
    return {
        "file": path.name,
        "image": meta,
        "query": query,
        "answer_fragments": fragments,
        "count": len(fragments),
        "retrieval_mode": mode,
        "embedding_model": settings.EMBEDDING_MODEL if mode in ("embedding", "milvus") else "",
        "retrieval_fallback_reason": fallback_reason,
        "degraded": not fragments,
        "degraded_reason": (
            f"图片《{path.name}》识别文本中没有与问题相关的段落：请换个说法"
            if not fragments
            else ""
        ),
        "language": language,
        "source": source,
        "fetched_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """图片内容问答的 ToolSpec（只读，免审）。"""
    return (
        ToolSpec(
            name="office.image.ask",
            scope=SCOPE_READ,
            description="图片内容问答：对文档工作目录内图片先 OCR 取真实文本再按段落检索摘录原文片段，回答「这张截图里 X 是什么」；引擎缺失/识别为空/零命中如实 degraded 不编造；配 EMBEDDING_* 走向量语义检索，失败回退字符检索并如实标注",
            params={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "图片文件名（不含路径，png/jpg/jpeg/bmp/gif/webp）",
                        "minLength": 1,
                        "maxLength": 120,
                    },
                    "query": {
                        "type": "string",
                        "description": "针对图片的问题（如：截图里的报错码是什么）",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "top_k": {"type": "integer", "description": "返回命中片段数（1-10，默认 3）"},
                    "language": {
                        "type": "string",
                        "description": "识别语言（Tesseract 语言码，默认 chi_sim+eng）",
                        "maxLength": 40,
                    },
                },
                "required": ["file_path", "query"],
                "additionalProperties": False,
            },
            handler=_image_ask,
        ),
    )


def register_all() -> list[str]:
    """注册图片内容问答工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
