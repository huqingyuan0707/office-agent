"""文档对比工具（office.doc.compare：两份 docx 段落级 diff + 变更摘要）。

职责：
- office.doc.compare（office:read）：读取 DOCS_DIR 内两份 .docx 的段落文本，
  用 difflib 做段落级比对，输出新增/删除/修改清单与变更摘要（只报实测差异，不解读语义）。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler；
磁盘 IO 走 asyncio.to_thread（不阻塞事件循环）。
红线：python-docx 缺失则不注册（宁缺席，不注册注定调不通的工具）；
      文件名经 paths.resolve_under_docs 锁进 DOCS_DIR，绝不越界。
对齐：AGENTS.md §3（分层红线）；智能办公Agent 产品需求文档.md §5.1（V1.0 文档对比）。
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from .paths import resolve_under_docs

logger = logging.getLogger(__name__)

try:  # 可选依赖：缺库不注册
    import docx  # python-docx
except ImportError:  # pragma: no cover
    docx = None  # type: ignore[assignment]

SCOPE_READ = "office:read"
_MAX_DETAILS = 50  # 每类差异明细条数上限（防大文档拖垮响应）


def _match_replace_block(
    old_items: list[str],
    new_items: list[str],
    *,
    line_offset_a: int,
    line_offset_b: int,
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    changed: list[dict[str, Any]],
) -> None:
    """replace 块内按文本相似度二次配对：相似（ratio≥0.6）算「修改」，
    配不上的旧段落算「删除」、新段落算「新增」——避免把“一改一增”误报成两处修改。"""
    used_new = [False] * len(new_items)
    for offset, old_text in enumerate(old_items):
        best_index, best_ratio = -1, 0.0
        for new_index, new_text in enumerate(new_items):
            if used_new[new_index]:
                continue
            ratio = difflib.SequenceMatcher(None, old_text, new_text).ratio()
            if ratio > best_ratio:
                best_index, best_ratio = new_index, ratio
        if best_index >= 0 and best_ratio >= 0.6:
            used_new[best_index] = True
            changed.append(
                {
                    "line_a": line_offset_a + offset + 1,
                    "old": old_text,
                    "new": new_items[best_index],
                }
            )
        else:
            removed.append({"line_a": line_offset_a + offset + 1, "text": old_text})
    for new_index, new_text in enumerate(new_items):
        if not used_new[new_index]:
            added.append({"line_b": line_offset_b + new_index + 1, "text": new_text})


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _read_paragraphs(path: Any) -> list[str]:
    """读取 docx 非空段落文本（原值直出，不做任何改写）。"""
    document = docx.Document(str(path))
    return [p.text.strip() for p in document.paragraphs if p.text.strip()]


async def _doc_compare(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.doc.compare：段落级 diff（新增/删除/修改），输出清单与摘要。"""
    _ = ctx
    file_a = str(args.get("file_a") or "").strip()
    file_b = str(args.get("file_b") or "").strip()
    if not file_a or not file_b:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, "参数 file_a 与 file_b 均不能为空：请传入两份 docx 文件名"
        )

    path_a = resolve_under_docs(file_a, (".docx",))
    path_b = resolve_under_docs(file_b, (".docx",))
    for path in (path_a, path_b):
        if not path.exists():
            raise BusinessError(
                ErrorCode.NOT_FOUND, f"文件不存在：{path.name}（文档工作目录内未找到该文件）", 404
            )

    paragraphs_a, paragraphs_b = await asyncio.to_thread(
        lambda: (_read_paragraphs(path_a), _read_paragraphs(path_b))
    )

    matcher = difflib.SequenceMatcher(a=paragraphs_a, b=paragraphs_b, autojunk=False)
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    unchanged = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            unchanged += i2 - i1
        elif tag == "insert":
            added += [
                {"line_b": j1 + offset + 1, "text": text}
                for offset, text in enumerate(paragraphs_b[j1:j2])
            ]
        elif tag == "delete":
            removed += [
                {"line_a": i1 + offset + 1, "text": text}
                for offset, text in enumerate(paragraphs_a[i1:i2])
            ]
        elif tag == "replace":
            _match_replace_block(
                paragraphs_a[i1:i2],
                paragraphs_b[j1:j2],
                line_offset_a=i1,
                line_offset_b=j1,
                added=added,
                removed=removed,
                changed=changed,
            )

    summary = f"两份文档共有段落 {unchanged} 段；实测差异：新增 {len(added)} 段、删除 {len(removed)} 段、修改 {len(changed)} 段"
    return {
        "file_a": path_a.name,
        "file_b": path_b.name,
        "counts": {
            "paragraph_a": len(paragraphs_a),
            "paragraph_b": len(paragraphs_b),
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": unchanged,
        },
        "change_summary": summary,
        "details": {
            "added": added[:_MAX_DETAILS],
            "removed": removed[:_MAX_DETAILS],
            "changed": changed[:_MAX_DETAILS],
        },
        "truncated": any(len(x) > _MAX_DETAILS for x in (added, removed, changed)),
        "source": f"local-docs:{path_a.name} vs {path_b.name}",
        "extracted_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """office.doc.compare 的 ToolSpec（python-docx 缺失时不注册）。"""
    if docx is None:
        return ()
    return (
        ToolSpec(
            name="office.doc.compare",
            scope=SCOPE_READ,
            description="文档对比：对两份 .docx 做段落级 diff，输出新增/删除/修改清单与变更摘要（只报实测差异，不做语义解读；仅限文档工作目录内）",
            params={
                "type": "object",
                "properties": {
                    "file_a": {
                        "type": "string",
                        "description": "旧版文件名（不含路径，.docx）",
                        "minLength": 1,
                        "maxLength": 120,
                    },
                    "file_b": {
                        "type": "string",
                        "description": "新版文件名（不含路径，.docx）",
                        "minLength": 1,
                        "maxLength": 120,
                    },
                },
                "required": ["file_a", "file_b"],
                "additionalProperties": False,
            },
            handler=_doc_compare,
        ),
    )


def register_all() -> list[str]:
    """注册文档对比工具（依赖缺失时返回空列表）；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
