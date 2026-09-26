"""图片 OCR 识别工具（ocr.image：Pillow 元数据直读 + 可选 OCR 引擎）。

职责：
- ocr.image（office:read）：读取 DOCS_DIR 内图片，返回尺寸/格式/模式等真实元数据；
  检测到 pytesseract + Tesseract 引擎时做真实文字识别；引擎不可用走降级
  （degraded=True，只回元数据与可操作提示），绝不 500、绝不编造识别文本。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：Pillow 缺失则整个工具不注册（宁缺席不注册注定调不通的工具）；
      识别文本只来自 OCR 引擎实测输出，引擎缺失时 text 置空并如实标注。
对齐：AGENTS.md §3（降级绝不 500）；智能办公Agent 产品需求文档.md §5.1（V1.0 图片OCR识别）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from PIL import Image

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from .paths import image_suffixes, resolve_under_docs

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

try:  # 可选依赖：pytesseract 缺失不阻断注册（运行时按 degraded 降级）
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None  # type: ignore[assignment]


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ocr_available() -> tuple[bool, str]:
    """探测 OCR 引擎可用性，返回 (是否可用, 不可用原因)。"""
    if pytesseract is None:
        return False, "pytesseract 未安装（pip install pytesseract 后重启服务）"
    try:
        pytesseract.get_tesseract_version()
    except Exception:  # Tesseract 二进制缺失/异常统一按不可用处理
        return False, "Tesseract 引擎未安装或不在 PATH（安装后重启服务即可启用真实识别）"
    return True, ""


def ocr_available() -> tuple[bool, str]:
    """OCR 引擎可用性公开探针（office.image.ask 复用同一判定，不重复实现）。"""
    return _ocr_available()


async def _ocr_image(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """ocr.image：图片元数据直读 + 可选引擎识别（引擎不可用走 degraded）。"""
    _ = ctx
    path = resolve_under_docs(str(args.get("file_path") or ""), image_suffixes())
    if not path.exists():
        raise BusinessError(
            ErrorCode.NOT_FOUND, f"图片不存在：{path.name}（文档工作目录内未找到该文件）", 404
        )
    lang = str(args.get("language") or "chi_sim+eng").strip()

    def _read_image() -> dict[str, Any]:
        with Image.open(path) as img:
            meta = {
                "format": img.format or "",
                "width": img.width,
                "height": img.height,
                "mode": img.mode,
            }
        return meta

    meta = await asyncio.to_thread(_read_image)

    available, reason = await asyncio.to_thread(_ocr_available)
    text = ""
    degraded = True
    if available:
        try:
            text = await asyncio.to_thread(pytesseract.image_to_string, str(path), lang=lang)  # type: ignore[union-attr]
            degraded = False
        except Exception as exc:  # 识别失败按降级处理，绝不 500
            logger.warning("OCR 识别失败（降级返回元数据）：%s", str(exc)[:120])
            reason = f"识别执行失败：{str(exc)[:100]}"

    return {
        "file": path.name,
        "image": meta,
        "text": text.strip(),
        "degraded": degraded,
        "degraded_reason": reason,
        "language": lang,
        "source": f"local-docs:{path.name}",
        "extracted_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """ocr.image 的 ToolSpec（Pillow 缺失时本模块不会被注册——import 即失败）。"""
    return (
        ToolSpec(
            name="ocr.image",
            scope=SCOPE_READ,
            description="图片OCR识别：返回图片真实元数据（格式/尺寸/色深），检测到 Tesseract 引擎时输出识别文本；引擎不可用走降级只回元数据并给出安装提示，绝不编造文字",
            params={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "图片文件名（不含路径，限 png/jpg/jpeg/bmp/gif/webp）",
                        "minLength": 1,
                        "maxLength": 120,
                    },
                    "language": {
                        "type": "string",
                        "description": "识别语言（Tesseract 语言码，默认 chi_sim+eng）",
                        "maxLength": 40,
                    },
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
            handler=_ocr_image,
        ),
    )


def register_all() -> list[str]:
    """注册 OCR 工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
