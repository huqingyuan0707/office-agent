"""跨源联合检索（office.kb.search_unified，对齐 PRD §2.6「跨源联合检索」）。

职责：office.kb.search_unified（office:read，免审）一句话联查五本地源——
  knowledge（kb.load_entries 同一口径）、docs（file_read.extract_document 同一口径）、
  affairs（本人待办/日程，tenant + owner/参会人过滤）、approvals（本人单或 admin/approver）、
  data（data_analysis 同一装载器）；各源双通道检索后按分合并全局 top_k，只摘录原文。
  权限：knowledge/docs 检索前按 kb.can_view 过滤（被滤只计 permission_filtered 不外泄）；
  affairs/approvals 按身份过滤；data 为演示公共数据。
  边界：聊天记录/OA 远端需经 LINKAGE_PROVIDERS 白名单接入，本仓无源不造假，接入后按 origin 追加。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler →
      kb/file_read/affairs/approval_submit/data_analysis（装载）→ retrieval（检索）。
红线：纯本地实现，不触及 ORM / FastAPI；外部依赖不可用只降级对应源，绝不 500；
      远端聊天/OA 不在本地编造（缺源如实标注，不伪装成有结果）。
对齐：AGENTS.md §3（分层/降级不 500/数据不出域/溯源）；
      智能办公Agent 产品需求文档.md §2.6（跨源联合检索/权限适配）、§4.1（跨源权限）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings
from office_agent_tools_office.retrieval import (
    chunks_with_meta,
    first_snippet,
    retrieve,
)

from . import approval_submit, data_analysis, file_read, kb
from .affairs import load_affairs

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

_ALL_SOURCES = ("knowledge", "docs", "affairs", "approvals", "data")
_MAX_DOC_FILES = 20
_MAX_DATA_ROWS = 50
_MAX_AFFAIRS_ITEMS = 100


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _sources_or_raise(raw: Any) -> list[str]:
    """sources 入参口径：缺省全源；显式传必须是非空允许枚举子集（未知源 1001）。"""
    if raw is None:
        return list(_ALL_SOURCES)
    if not isinstance(raw, list) or not raw:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 sources 必须是非空数组（子集）")
    picked: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name not in _ALL_SOURCES:
            raise BusinessError(ErrorCode.PARAM_INVALID, f"未知检索源：{item}")
        if name not in picked:
            picked.append(name)
    return picked


async def _retrieve_chunks(
    query: str, chunks: list[dict[str, str]], top_k: int, origin: str
) -> tuple[list[tuple[float, dict[str, str]]], str, str]:
    """单源检索：委托 retrieval.retrieve 三级降级链（milvus → embedding → bigram）。

    origin 传给向量库做标量过滤，五源各自只查本源（跨源合并与排序由调用方负责）。
    """
    return await retrieve(query, chunks, top_k, origin=origin)


def _format_hit(score: float, chunk: dict[str, str], query: str, mode: str) -> dict[str, Any]:
    """命中块 → 出参条目（只摘录原文片段，分数原样暴露供自判强弱）。"""
    return {
        "title": chunk["title"],
        "snippet": first_snippet(chunk["text"], query, mode),
        "score": round(score, 4),
        "source": chunk["source"],
        "origin": chunk.get("origin", ""),
    }


# ---------------- 各源装载（只组装有权见的数据） ----------------


async def _knowledge_entries(roles: list[str]) -> tuple[list[dict[str, str]], bool, int]:
    """知识源：kb 同一装载口径 + visibility 过滤；返回 (条目, 目录缺失, 被滤数)。"""
    entries, dir_missing = await asyncio.to_thread(kb.load_entries)
    visible = [
        {**entry, "origin": "knowledge"}
        for entry in entries
        if kb.can_view(str(entry.get("visibility", kb.VISIBILITY_PUBLIC)), roles)
    ]
    return visible, dir_missing, len(entries) - len(visible)


def _list_doc_files() -> list[Path]:
    """列出 DOCS_DIR 顶层可检索文档（同步 IO，调用方包 to_thread）。"""
    root = Path(settings.DOCS_DIR)
    if not root.is_dir():
        return []
    try:
        files = sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in file_read.READ_SUFFIXES
        )
    except OSError as exc:
        logger.warning("联合检索文档目录不可读：%s", str(exc)[:120])
        return []
    return files[:_MAX_DOC_FILES]


def _read_doc_text(path: Path) -> str | None:
    """抽取单文件文本（同步 IO，调用方包 to_thread；超限/失败/无文字层返回 None）。"""
    try:
        if path.stat().st_size > file_read.MAX_FILE_BYTES:
            return None
        extracted = file_read.extract_document(path)
    except (OSError, ValueError) as exc:
        logger.warning("联合检索跳过 %s：%s", path.name, str(exc)[:120])
        return None
    text = str(extracted.get("text") or "").strip()
    if extracted.get("degraded") or not text:
        return None
    return text


async def _docs_entries(roles: list[str]) -> tuple[list[dict[str, str]], int, int]:
    """文档源逐文件抽取（`restricted-` 前缀受限，读盘前过滤；失败只跳过）→ (条目, 被滤, 跳过)。"""
    files = await asyncio.to_thread(_list_doc_files)
    entries: list[dict[str, str]] = []
    filtered = 0
    skipped = 0
    for path in files:
        visibility = "restricted" if path.name.startswith("restricted-") else kb.VISIBILITY_PUBLIC
        if not kb.can_view(visibility, roles):
            filtered += 1
            continue
        text = await asyncio.to_thread(_read_doc_text, path)
        if text is None:
            skipped += 1
            continue
        entries.append(
            {
                "title": path.name,
                "content": text,
                "source": f"local-docs:{path.name}",
                "visibility": visibility,
                "origin": "docs",
            }
        )
    return entries, filtered, skipped


async def _affairs_entries(ctx: ToolContext) -> list[dict[str, str]]:
    """事务源：只装本人待办/日程（owner==本人，或日程参会人含本人）。"""
    try:
        data = await load_affairs(ctx.tenant)
    except BusinessError:
        return []
    entries: list[dict[str, str]] = []
    for todo in data.get("todos", [])[:_MAX_AFFAIRS_ITEMS]:
        if todo.get("owner") != ctx.username:
            continue
        entries.append(
            {
                "title": str(todo.get("title") or "未命名待办"),
                "content": "待办｜标题：{}｜状态：{}｜优先级：{}｜截止：{}｜描述：{}".format(
                    todo.get("title", ""),
                    todo.get("status", ""),
                    todo.get("priority", ""),
                    todo.get("due_date") or "无",
                    todo.get("description") or "无",
                ),
                "source": f"local-affairs:todo:{todo.get('id', '')}",
                "visibility": kb.VISIBILITY_PUBLIC,
                "origin": "affairs",
            }
        )
    for schedule in data.get("schedules", [])[:_MAX_AFFAIRS_ITEMS]:
        attendees = schedule.get("attendees") or []
        if schedule.get("owner") != ctx.username and ctx.username not in attendees:
            continue
        entries.append(
            {
                "title": str(schedule.get("title") or "未命名日程"),
                "content": "日程｜标题：{}｜类型：{}｜开始：{}｜结束：{}｜地点：{}｜参会人：{}".format(
                    schedule.get("title", ""),
                    schedule.get("kind", ""),
                    schedule.get("start", ""),
                    schedule.get("end") or "无",
                    schedule.get("location") or "无",
                    "、".join(attendees) if attendees else "无",
                ),
                "source": f"local-affairs:schedule:{schedule.get('id', '')}",
                "visibility": kb.VISIBILITY_PUBLIC,
                "origin": "affairs",
            }
        )
    return entries


async def _approvals_entries(ctx: ToolContext) -> list[dict[str, str]]:
    """单据源：审批台账按身份过滤（本人单，或 admin/approver/* 可见全租户单）。"""
    try:
        tickets = await asyncio.to_thread(approval_submit._read_ledger)
    except BusinessError:
        return []
    privileged = any(role in ("*", "admin", "approver") for role in ctx.roles)
    entries: list[dict[str, str]] = []
    for ticket in tickets:
        if ticket.get("tenant") != ctx.tenant:
            continue
        if ticket.get("applicant") != ctx.username and not privileged:
            continue
        fields = ticket.get("fields") or {}
        flat = "｜".join(f"{key}={value}" for key, value in fields.items()) or "无字段"
        entries.append(
            {
                "title": "{}单据（{}）".format(
                    ticket.get("kind_label") or ticket.get("kind", ""),
                    str(ticket.get("ticket_id", ""))[:8],
                ),
                "content": "单据｜类型：{}｜申请人：{}｜状态：{}｜字段：{}｜提交：{}".format(
                    ticket.get("kind_label") or ticket.get("kind", ""),
                    ticket.get("applicant", ""),
                    ticket.get("status", ""),
                    flat,
                    ticket.get("submitted_at", ""),
                ),
                "source": f"local-ledger:{ticket.get('ticket_id', '')}",
                "visibility": kb.VISIBILITY_PUBLIC,
                "origin": "approvals",
            }
        )
    return entries


async def _data_entries() -> list[dict[str, str]]:
    """台账源：四类演示台账 + CSV 叠加逐行成条（演示公共数据，无身份过滤）。"""
    entries: list[dict[str, str]] = []
    for dataset, base_rows in data_analysis._DATASETS.items():
        label = data_analysis._DATASET_LABELS.get(dataset, dataset)
        try:
            overlay, _ = await asyncio.to_thread(data_analysis._load_csv_overlay, dataset)
        except Exception as exc:  # 叠加读失败只用内置行，绝不炸整源
            logger.warning("联合检索台账叠加跳过 %s：%s", dataset, str(exc)[:120])
            overlay = []
        for row in (base_rows + overlay)[:_MAX_DATA_ROWS]:
            flat = "｜".join(f"{key}={value}" for key, value in row.items())
            head = next((str(value) for value in row.values() if str(value).strip()), dataset)
            entries.append(
                {
                    "title": f"{label}｜{head}",
                    "content": f"{label}｜{flat}",
                    "source": f"local-ledger:{dataset}",
                    "visibility": kb.VISIBILITY_PUBLIC,
                    "origin": "data",
                }
            )
    return entries


async def _search_unified(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.kb.search_unified：五源装载 → 各源双通道检索 → 全局合并排序。"""
    query = str(args.get("query") or "").strip()
    if not query:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 query 不能为空：请输入要查的问题")
    top_k = args.get("top_k", 3)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 10:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 top_k 必须是 1-10 的整数")
    sources = _sources_or_raise(args.get("sources"))
    roles = list(ctx.roles)

    loaded: dict[str, list[dict[str, str]]] = {}
    permission_filtered = 0
    notes: dict[str, str] = {}
    if "knowledge" in sources:
        items, dir_missing, filtered = await _knowledge_entries(roles)
        loaded["knowledge"] = items
        permission_filtered += filtered
        if dir_missing:
            notes["knowledge"] = "知识目录缺失，仅检索内置条目"
    if "docs" in sources:
        items, filtered, skipped = await _docs_entries(roles)
        loaded["docs"] = items
        permission_filtered += filtered
        if skipped:
            notes["docs"] = f"{skipped} 个文件无文字层/超限已跳过（扫描件请走 office.image.ask）"
    if "affairs" in sources:
        loaded["affairs"] = await _affairs_entries(ctx)
    if "approvals" in sources:
        loaded["approvals"] = await _approvals_entries(ctx)
    if "data" in sources:
        loaded["data"] = await _data_entries()

    per_source: dict[str, Any] = {}
    all_scored: list[tuple[float, dict[str, str]]] = []
    mode = "bigram"
    fallbacks: list[str] = []
    for origin in sources:
        chunks = chunks_with_meta(loaded.get(origin, []))
        if not chunks:
            per_source[origin] = {"count": 0, "results": [], "note": "本源无可见内容"}
            continue
        scored, used, fallback = await _retrieve_chunks(query, chunks, top_k, origin)
        mode = used if len(all_scored) == 0 else mode
        if fallback and fallback not in fallbacks:
            fallbacks.append(fallback)
        hits = [_format_hit(score, chunk, query, used) for score, chunk in scored]
        per_source[origin] = {"count": len(hits), "results": hits}
        all_scored.extend(scored)
    all_scored.sort(key=lambda pair: pair[0], reverse=True)
    merged = [_format_hit(score, chunk, query, mode) for score, chunk in all_scored[:top_k]]
    degraded = not merged
    return {
        "query": query,
        "sources": sources,
        "per_source": per_source,
        "merged": merged,
        "merged_count": len(merged),
        "retrieval_mode": mode,
        "embedding_model": settings.EMBEDDING_MODEL if mode in ("embedding", "milvus") else "",
        "retrieval_fallback_reason": "；".join(fallbacks),
        "permission_filtered": permission_filtered,
        "notes": notes,
        "degraded": degraded,
        "degraded_reason": "五个来源均无命中：请换个说法，或确认内容已入库" if degraded else "",
        "source": "local-unified",
        "fetched_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """跨源联合检索的 ToolSpec（只读，免审）。"""
    return (
        ToolSpec(
            name="office.kb.search_unified",
            scope=SCOPE_READ,
            description="跨源联合检索：一次问句同时查知识库（内置制度+KB_DIR，visibility 权限过滤）/网盘文档（DOCS_DIR，restricted- 前缀受限）/本人待办日程（身份过滤）/审批单据台账（本人或管理员）/项目数据台账四类；各源双通道检索后按分数合并，全程只摘录原文，被滤条目只计数不外泄；聊天记录/OA 远端需经联动白名单接入，本地无源不造假",
            params={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要查的问题（如：报销审批要找谁）",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "每源与合并各返回条数（1-10，默认 3）",
                    },
                    "sources": {
                        "type": "array",
                        "description": "限定检索源（缺省全源）",
                        "items": {"type": "string"},
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_search_unified,
        ),
    )


def register_all() -> list[str]:
    """注册跨源联合检索工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
