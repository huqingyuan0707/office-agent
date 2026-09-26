"""知识库后台文档管理（PRD §2.13：上传、解析、向量化、统计、按源删除）。

职责（纯本地实现，不触 ORM / FastAPI；存储层原语在同包 kb_store）：
- 入盘 save_and_ingest：上传字节落进 KB_DIR——basename 化 + 后缀白名单 + 体积上限，
  越界/超限一律 1001（复用 paths.resolve_under_kb，与 office.file.read 同一守卫口径）；
- 解析 + 切块：复用 kb_store.extract_text（pdf/docx/xlsx/csv/txt/md 同一提取口径）
  与 retrieval.chunks_with_meta（与 kb.ask 检索同一粒度——同一段落块，入库即入库到检索口径）；
- 向量化 _vectorize：向量库可用即显式灌库（retrieval.index_chunks，origin=knowledge、
  source=local-kb:<名>，与 kb.load_entries 的来源约定一致，按源删除才对得上）；
  未配 EMBEDDING_* / 未配 MILVUS_URI / 向量库不可用一律如实标注 vector_mode，
  绝不把「检索时按需算」说成「已落库」；
- 统计：来源清单（kb_store）× 真实目录联查——「已入盘未解析」「清单在但文件缺失」
  都如实标 state，不掩盖；
- 按源删除 delete_source：删文件 + 删该源全部向量块 + 清清单条目。

链路：server api/kb_admin → services/kb_admin → 本模块 → kb_store / retrieval / vector_store。
红线：文件名锁 KB_DIR；解析引擎与向量库不可用只降级标注，绝不 500；
      本模块只做确定性转换与原样摘录，不改写原文。
对齐：AGENTS.md §3（分层红线 / 降级绝不 500 / 零硬编码）；
      智能办公Agent 产品需求文档.md §2.13（知识库后台）、§2.6（知识库增强检索）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import settings
from office_agent_tools_office import file_read, retrieval, vector_store
from office_agent_tools_office.kb_store import (
    KB_SUFFIXES,
    ORIGIN,
    extract_text,
    kb_root,
    list_kb_files,
    load_manifest,
    manifest_visibility,
    patch_manifest,
    source_key,
    title_from_text,
    utc_now_text,
    visibility_or_raise,
)
from office_agent_tools_office.paths import resolve_under_kb

logger = logging.getLogger(__name__)

__all__ = [
    "KB_SUFFIXES",
    "MAX_UPLOAD_BYTES",
    "ORIGIN",
    "delete_source",
    "ingest_source",
    "kb_stats",
    "list_sources",
    "manifest_visibility",
    "save_and_ingest",
]

#: 单次上传体积上限（**与解析上限同一出处**：放宽上传却解析不了，等于把「已上传」
#: 说成「已入库」——故此处直接取 file_read.MAX_FILE_BYTES，不做第二个魔数）
MAX_UPLOAD_BYTES = file_read.MAX_FILE_BYTES


async def _vectorize(chunks: list[dict[str, str]]) -> tuple[bool, str, str]:
    """段落块增量落向量库，返回 (是否已落库, 模式, 说明)。

    模式口径：milvus=已落库；on_demand_embedding=检索时按需算（未配向量库或向量库不可用）；
    unconfigured=连 embedding 都没配（检索走字符通道）。
    """
    if not retrieval.embedding_ready():
        return False, "unconfigured", "未配置 EMBEDDING_*（向量检索整体关闭，检索走字符通道）"
    if vector_store.get_store() is None:
        return (
            False,
            "on_demand_embedding",
            "未配置 MILVUS_URI：语义检索可用，但向量在检索时按需计算、不落库",
        )
    try:
        written = await retrieval.index_chunks(chunks, ORIGIN)
    except Exception as exc:  # 对端离线/超时/响应非法一律降级标注，绝不 500
        reason = f"向量库不可用已回退按需向量化：{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning("知识库向量化降级 %s", reason)
        return False, "on_demand_embedding", reason
    return True, "milvus", f"已落库 {written} 个新增块（同文本块跳过）"


async def save_and_ingest(
    filename: str, data: bytes, *, uploaded_by: str = "", visibility: str = "public"
) -> dict[str, Any]:
    """上传入盘 + 立刻解析向量化（知识库后台「资料上传」唯一入口）。

    重传同名文件：先删该源历史向量再重新入库（否则改过文本的旧块会留在库里积垃圾）。
    """
    _visibility = visibility_or_raise(visibility)
    if not data:
        raise BusinessError(ErrorCode.PARAM_INVALID, "上传内容为空：请重新选择文件")
    if len(data) > MAX_UPLOAD_BYTES:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"文件过大（{len(data)} 字节，上限 {MAX_UPLOAD_BYTES}）：请拆分后再上传",
        )
    path = resolve_under_kb(filename, KB_SUFFIXES)
    name = path.name
    async with asyncio.Lock():
        await asyncio.to_thread(path.write_bytes, data)
    reuploaded = bool((await asyncio.to_thread(load_manifest)).get(name))
    if reuploaded:
        await _clear_vectors(name)
    result = await ingest_source(name, uploaded_by=uploaded_by, visibility=_visibility)
    return {**result, "size_bytes": len(data), "reuploaded": reuploaded}


async def ingest_source(
    filename: str, *, uploaded_by: str = "", visibility: str = ""
) -> dict[str, Any]:
    """解析 + 切块 + 向量化一个已在 KB_DIR 的来源，并把元数据写进清单。

    只读盘不写盘（落盘由 save_and_ingest 负责），因此也可对「手工放进目录」的文件补解析。
    """
    path = resolve_under_kb(filename, KB_SUFFIXES)
    if not path.is_file():
        raise BusinessError(ErrorCode.NOT_FOUND, f"知识库中不存在文件：{path.name}", 404)
    size = path.stat().st_size
    _visibility = visibility_or_raise(visibility) if visibility else "public"
    base = {
        "filename": path.name,
        "format": path.suffix.lower().lstrip("."),
        "size_bytes": size,
        "visibility": _visibility,
        "source": source_key(path.name),
        "fetched_at": utc_now_text(),
    }
    if size > file_read.MAX_FILE_BYTES:
        return await _record_degraded(
            base,
            "skipped",
            f"文件 {size} 字节超过解析上限 {file_read.MAX_FILE_BYTES}：已入盘未解析，"
            "请拆分后再上传",
            uploaded_by=uploaded_by,
        )
    extracted = await extract_text(path)
    text = str(extracted.get("text") or "").strip()
    if extracted.get("degraded") or not text:
        reason = str(extracted.get("degraded_reason") or "文件无可用文字层（扫描件请走图片问答）")
        return await _record_degraded(base, "unparsed", reason, uploaded_by=uploaded_by)
    entry = {
        "title": title_from_text(text, path.stem),
        "content": text,
        "source": source_key(path.name),
        "visibility": _visibility,
    }
    chunks = retrieval.chunks_with_meta([entry])
    vectorized, vector_mode, vector_reason = await _vectorize(chunks)
    await patch_manifest(
        path.name,
        {
            "title": entry["title"],
            "format": base["format"],
            "size_bytes": size,
            "chunks": len(chunks),
            "vectorized": vectorized,
            "vector_mode": vector_mode,
            "vector_reason": vector_reason,
            "visibility": _visibility,
            "uploaded_by": uploaded_by,
            "uploaded_at": base["fetched_at"],
            "degraded": False,
            "degraded_reason": "",
        },
    )
    return {
        **base,
        "title": entry["title"],
        "chunks": len(chunks),
        "vectorized": vectorized,
        "vector_mode": vector_mode,
        "vector_reason": vector_reason,
        "truncated": bool(extracted.get("truncated")),
        "degraded": False,
        "degraded_reason": "",
        "note": "已解析并切块入库；检索（kb.ask / 跨源联查）即刻可见",
    }


async def _record_degraded(
    base: dict[str, Any], mode: str, reason: str, *, uploaded_by: str
) -> dict[str, Any]:
    """解析不可用时的如实入账：文件在盘上可查（state=unindexed），但切片数 0、原因写清。"""
    await patch_manifest(
        base["filename"],
        {
            "title": base["filename"],
            "format": base["format"],
            "size_bytes": base["size_bytes"],
            "chunks": 0,
            "vectorized": False,
            "vector_mode": mode,
            "vector_reason": reason,
            "visibility": base["visibility"],
            "uploaded_by": uploaded_by,
            "uploaded_at": base["fetched_at"],
            "degraded": True,
            "degraded_reason": reason,
        },
    )
    return {
        **base,
        "title": base["filename"],
        "chunks": 0,
        "vectorized": False,
        "vector_mode": mode,
        "vector_reason": reason,
        "truncated": False,
        "degraded": True,
        "degraded_reason": reason,
        "note": "文件已入盘但未解析：检索不会命中该文件内容",
    }


async def list_sources() -> dict[str, Any]:
    """来源清单：清单元数据 × 真实目录联查（已入盘未解析 / 清单在但文件缺失都如实标 state）。

    summary 只统计**现役来源**（state != missing）：文件已不在盘上，其清单里的历史切片数
    与向量化标记就不再计入「当前可检索量」，否则统计会虚高；每行仍按清单原值展示，
    状态标签与原因如实说明。
    """
    root = kb_root()
    manifest, present = await asyncio.gather(
        asyncio.to_thread(load_manifest), asyncio.to_thread(list_kb_files, root)
    )
    names = sorted(set(manifest) | set(present))
    items = [_source_row(name, manifest.get(name) or {}, present) for name in names]
    live = [item for item in items if item["state"] != "missing"]
    summary = {
        "files": len(live),
        "indexed": sum(1 for item in live if item["state"] == "indexed"),
        "unindexed": sum(1 for item in live if item["state"] == "unindexed"),
        "missing": sum(1 for item in items if item["state"] == "missing"),
        "chunks": sum(item["chunks"] for item in live),
        "vectorized_files": sum(1 for item in live if item["vectorized"]),
        "vectorized_chunks": sum(item["chunks"] for item in live if item["vectorized"]),
        "total_bytes": sum(item["size_bytes"] for item in live),
    }
    return {
        "items": items,
        "total": len(items),
        "summary": summary,
        "supported_formats": [suffix.lstrip(".") for suffix in KB_SUFFIXES],
        "store": _store_status(),
        "kb_dir": settings.KB_DIR,
        "source": "local-kb-admin",
        "fetched_at": utc_now_text(),
    }


def _source_row(name: str, meta: dict[str, Any], present: dict[str, int]) -> dict[str, Any]:
    """单条来源行：磁盘实况优先，清单补元数据；两侧不一致即如实标 state。"""
    on_disk = name in present
    if not on_disk:
        state = "missing"
    elif not meta:
        state = "unindexed"
    else:
        state = "indexed"
    return {
        "filename": name,
        "title": str(meta.get("title") or name),
        "format": str(meta.get("format") or Path(name).suffix.lower().lstrip(".")),
        "size_bytes": int(present.get(name, meta.get("size_bytes") or 0)),
        "chunks": int(meta.get("chunks") or 0),
        "vectorized": bool(meta.get("vectorized")),
        "vector_mode": str(meta.get("vector_mode") or "unindexed"),
        "vector_reason": str(meta.get("vector_reason") or ""),
        "visibility": str(meta.get("visibility") or "public"),
        "uploaded_by": str(meta.get("uploaded_by") or ""),
        "uploaded_at": str(meta.get("uploaded_at") or ""),
        "degraded": bool(meta.get("degraded")),
        "degraded_reason": str(meta.get("degraded_reason") or ""),
        "state": state,
        "state_label": {
            "indexed": "已入库",
            "unindexed": "已入盘未解析",
            "missing": "文件缺失",
        }[state],
    }


async def _clear_vectors(filename: str) -> int:
    """清某来源的全部向量块（重传前清旧块 / 按源删除共用）；向量库不可用返回 0。"""
    store = vector_store.get_store()
    if store is None:
        return 0
    try:
        return await store.delete_by_source(ORIGIN, source_key(filename))
    except Exception as exc:  # 向量库对端故障：不阻断文件层动作，如实告警
        logger.warning("清理知识库向量失败 %s：%s", filename, str(exc)[:120])
        return 0


async def delete_source(filename: str) -> dict[str, Any]:
    """按源删除：删文件 + 删该源全部向量块 + 清清单条目（文件与清单都不存在才 404）。"""
    path = resolve_under_kb(filename, KB_SUFFIXES)
    name = path.name
    manifest = await asyncio.to_thread(load_manifest)
    has_file = path.is_file()
    if not has_file and name not in manifest:
        raise BusinessError(
            ErrorCode.NOT_FOUND, f"知识库中不存在：{name}（已删除或文件名有误）", 404
        )
    removed_vectors = await _clear_vectors(name)
    if has_file:
        await asyncio.to_thread(path.unlink)
    await patch_manifest(name, None)
    return {
        "filename": name,
        "deleted": True,
        "removed_vectors": removed_vectors,
        "vector_reason": _delete_vector_reason(removed_vectors),
        "note": "文件、来源清单与向量块已清理；检索不再命中该来源",
        "source": "local-kb-admin",
        "fetched_at": utc_now_text(),
    }


def _delete_vector_reason(removed_vectors: int) -> str:
    """删除时向量侧口径说明（无向量库不等于失败，如实说清）。"""
    if vector_store.get_store() is None:
        return "未配置 MILVUS_URI：向量未落库，无历史向量需清理"
    return f"已清理该来源向量 {removed_vectors} 块"


def _store_status() -> dict[str, str]:
    """向量存储当前口径（后台统计如实呈现，不把按需计算说成已落库）。"""
    if not retrieval.embedding_ready():
        return {"mode": "unconfigured", "reason": "未配置 EMBEDDING_*：检索走字符通道"}
    if vector_store.get_store() is None:
        return {
            "mode": "on_demand_embedding",
            "reason": "未配置 MILVUS_URI：向量在检索时按需计算，不入库",
        }
    return {
        "mode": "milvus",
        "reason": f"向量落库于 Milvus collection {settings.MILVUS_COLLECTION}",
    }


async def kb_stats() -> dict[str, Any]:
    """知识库统计：来源清单聚合 + 按格式分组 + 向量存储口径（全部来自真实清点）。"""
    listing = await list_sources()
    by_format: dict[str, dict[str, int]] = {}
    for item in listing["items"]:
        bucket = by_format.setdefault(item["format"], {"files": 0, "chunks": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["chunks"] += item["chunks"]
        bucket["bytes"] += item["size_bytes"]
    return {
        "summary": listing["summary"],
        "by_format": [{"format": name, **bucket} for name, bucket in sorted(by_format.items())],
        "store": listing["store"],
        "embedding_model": settings.EMBEDDING_MODEL if retrieval.embedding_ready() else "",
        "supported_formats": listing["supported_formats"],
        "kb_dir": listing["kb_dir"],
        "source": "local-kb-admin",
        "fetched_at": utc_now_text(),
    }
