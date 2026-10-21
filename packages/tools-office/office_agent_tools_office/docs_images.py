"""文档批量提取图片（office.docs.extract_images，PRD §2.1「文档批量处理：批量提取图片」）。

职责：
- office.docs.extract_images（office:write + 恒送审 + idem_key 必填）：把 docx/pptx
  内嵌的媒体文件批量导出到文档工作目录的 ``extracted/`` 子目录，逐张给出文件名与字节数。

实现口径：docx/pptx 都是 zip 容器，图片即容器内 ``word/media/*`` / ``ppt/media/*``
      条目——直接读 zip 条目即可，**零新依赖**（不引 python-pptx/python-docx 解析）；
      同一份文件重复导出覆盖同名文件（同内容同结果），故按幂等口径登记 idem_key。

红线：写动作恒送审（审批通过才落盘）；文件名与输出目录都锁进 DOCS_DIR（防路径穿越）；
      非 docx/pptx 1001、缺文件 404、容器内无图如实 0 张（不编造）。
对齐：AGENTS.md §3（写动作恒送审/数据不出域）；智能办公Agent 产品需求文档.md §2.1。
"""

from __future__ import annotations

import asyncio
import logging
import zipfile
from pathlib import Path
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings

from .paths import resolve_under_docs

logger = logging.getLogger(__name__)

SCOPE_WRITE = "office:write"

#: 支持的源格式与各自容器内的媒体目录（zip 条目前缀）
_MEDIA_DIRS: dict[str, str] = {
    ".docx": "word/media/",
    ".pptx": "ppt/media/",
}

#: 输出子目录名（相对 DOCS_DIR；basename 由源文件名派生，不接受外部路径）
_OUT_DIR = "extracted"

#: 单次最多导出的图片数（防超大容器把盘写满）
_MAX_IMAGES = 200


def _out_dir_for(source: Path) -> Path:
    """输出目录：DOCS_DIR/extracted/<源文件名去后缀>/（锁在 DOCS_DIR 内，防穿越）。"""
    root = Path(settings.DOCS_DIR).resolve()
    target = (root / _OUT_DIR / source.stem).resolve()
    if root not in target.parents:
        raise BusinessError(ErrorCode.PARAM_INVALID, "输出目录非法：不允许越界文档工作目录")
    return target


def _extract(source: Path) -> dict[str, Any]:
    """zip 容器内媒体条目批量导出（同步 IO，调用方 to_thread）。"""
    suffix = source.suffix.lower()
    media_prefix = _MEDIA_DIRS.get(suffix)
    if media_prefix is None:
        allowed = " / ".join(sorted(_MEDIA_DIRS))
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 filename 只支持 {allowed}（当前：{source.name}）"
        )
    if not source.is_file():
        raise BusinessError(
            ErrorCode.NOT_FOUND, f"文件不存在：{source.name}（请先放入文档工作目录）", 404
        )
    out_dir = _out_dir_for(source)
    extracted: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(source) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.startswith(media_prefix) and not name.endswith("/")
            ]
            for name in names[:_MAX_IMAGES]:
                target = out_dir / Path(name).name
                data = archive.read(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                extracted.append({"name": target.name, "bytes": len(data), "path": str(target)})
    except zipfile.BadZipFile as exc:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"文件不是有效的 Office 容器（zip 损坏）：{source.name}"
        ) from exc
    except OSError as exc:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"图片导出失败：{str(exc)[:120]}") from exc
    return {
        "source": source.name,
        "out_dir": str(out_dir),
        "extracted": extracted,
        "count": len(extracted),
        "truncated": len(names) > _MAX_IMAGES,
    }


async def _docs_extract_images(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.docs.extract_images：把文档内嵌图片批量导出到文档工作目录（审批通过才落盘）。"""
    _ = ctx
    idem_key = str(args.get("idem_key") or "").strip()
    if len(idem_key) < 8:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, "参数 idem_key 至少 8 字符（批量导出的幂等回放依据）"
        )
    source = resolve_under_docs(str(args.get("filename") or ""), tuple(sorted(_MEDIA_DIRS)))
    result = await asyncio.to_thread(_extract, source)
    return {
        **result,
        "idem_key": idem_key,
        "note": (
            "图片已导出到文档工作目录的 extracted/ 子目录（同内容重复导出会覆盖同名文件，"
            "不产生重复文件）；容器内无图时 count=0，如实返回不编造"
        ),
    }


def specs() -> tuple[ToolSpec, ...]:
    """批量提图工具的 ToolSpec（写口径：恒送审 + 幂等键必带）。"""
    return (
        ToolSpec(
            name="office.docs.extract_images",
            scope=SCOPE_WRITE,
            description="从 docx/pptx 批量导出内嵌图片（写动作）：恒送审 + idem_key 必填，"
            "审批通过后把容器内媒体文件写到文档工作目录的 extracted/ 子目录；"
            "非 docx/pptx 1001、缺文件 404、无图如实 0 张",
            params={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "源文件名（不含路径，.docx / .pptx）",
                        "minLength": 1,
                        "maxLength": 120,
                    },
                    "idem_key": {
                        "type": "string",
                        "description": "幂等键（8-64 字符）",
                        "minLength": 8,
                        "maxLength": 64,
                    },
                },
                "required": ["filename", "idem_key"],
                "additionalProperties": False,
            },
            handler=_docs_extract_images,
            idempotent=True,
            requires_approval=True,
            approval_action="office.docs.extract_images",
        ),
    )


def register_all() -> list[str]:
    """注册批量提图工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
