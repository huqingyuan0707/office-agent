"""企业知识库问答工具（kb.ask：双通道检索，制度答疑最小闭环）。

职责：
- kb.ask（office:read）：对「内置演示条目 + KB_DIR 本地文件（*.md/*.txt）」做检索，
  返回 top_k 命中片段，带 source + fetched_at 溯源；库为空时 degraded=True 留白不编造答案。
- 检索双通道：**向量语义检索**优先（Settings 的 EMBEDDING_* 配置本地 OpenAI 兼容
  /embeddings，按段落余弦相似度排序）→ 未配置或调用失败时回退**字符 bigram 检索**。
  降级只在结果里如实标注（retrieval_mode / retrieval_fallback_reason），绝不 500。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：纯本地实现，不触及 ORM / FastAPI；答案只摘录命中条目原文（不生成、不编造）。
      向量检索走本地私有化部署（数据不出域）；外部依赖不可用走降级路径，绝不 500。
对齐：AGENTS.md §3（分层红线 / 降级绝不 500 / 数据不出域）；
      智能办公Agent 产品需求文档.md §5.1（V1.0 知识库问答）、
      §2.6（制度答疑/资料检索/权限适配——权限经 office:read Scope 闸门统一控制）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

_KB_SUFFIXES = (".md", ".txt")
_SNIPPET_CHARS = 120
_MAX_FILE_BYTES = 200_000  # 单文件读取上限（防超大文件拖垮响应）

#: 向量检索的段落切分上限：长文整篇压成一个向量会丢语义（模型还会静默截断），故按段落切
_CHUNK_CHARS = 600
#: 单次 /embeddings 请求的最大文本条数（本地小模型一次吃太多会超时）
_EMBED_BATCH = 16

#: 内置演示条目（source=builtin-demo；真实部署用 KB_DIR 目录替换/追加）
_BUILTIN_ENTRIES: list[dict[str, str]] = [
    {
        "title": "考勤制度（演示条目）",
        "content": "工作日 9:00-18:00，弹性上班 8:30-10:00 之间到岗即视为正常；"
        "每月补卡不超过 3 次；连续迟到 3 次以上需向直属主管说明原因。",
    },
    {
        "title": "报销制度（演示条目）",
        "content": "报销单需在费用发生后 30 天内提交，附发票原件；"
        "单笔超过 1000 元需部门负责人审批，超过 5000 元需分管副总审批；"
        "报销周期为每周三统一打款。",
    },
    {
        "title": "请假流程（演示条目）",
        "content": "1 天以内请假由直属主管审批；3 天以内需提前 1 天申请；"
        "3 天以上需提前 3 个工作日申请并做好工作交接；病假需补交医院证明。",
    },
    {
        "title": "差旅标准（演示条目）",
        "content": "高铁二等座、经济舱为默认标准；住宿一线城市每晚上限 500 元，"
        "其他城市上限 350 元；市内交通实报实销，需保留行程凭证。",
    },
]


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _bigrams(text: str) -> set[str]:
    """中文友好分词：字符 bigram 集合（单字查询退化为单字集合）。"""
    cleaned = "".join(ch for ch in text.lower() if not ch.isspace())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def _score_entry(query_grams: set[str], entry_text: str) -> float:
    """命中率 = 命中的 query bigram 数 / query bigram 总数（0-1）。"""
    if not query_grams:
        return 0.0
    haystack = entry_text.lower()
    hit = sum(1 for gram in query_grams if gram in haystack)
    return hit / len(query_grams)


def _hard_wrap(text: str, limit: int) -> list[str]:
    """超长单段硬切（无空行的长文也要能被切分，否则整篇一个向量等于没检索）。"""
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [text]


def _chunk_entry(entry: dict[str, str]) -> list[dict[str, str]]:
    """一条条目 → 若干段落块（按空行切、合并到 _CHUNK_CHARS 以内；超长段硬切）。

    切块是向量检索的前提：整篇文本压成一个向量会丢语义，命中粒度也回不到段落。
    """
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", entry["content"]):
        stripped = paragraph.strip()
        if not stripped:
            continue
        pieces.extend(
            _hard_wrap(stripped, _CHUNK_CHARS) if len(stripped) > _CHUNK_CHARS else [stripped]
        )
    if not pieces:
        pieces = [entry["content"]]
    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if buffer and len(buffer) + len(piece) + 1 > _CHUNK_CHARS:
            chunks.append(buffer)
            buffer = piece
        else:
            buffer = f"{buffer}\n{piece}" if buffer else piece
    if buffer:
        chunks.append(buffer)
    return [{"title": entry["title"], "source": entry["source"], "text": text} for text in chunks]


def _chunks_of(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """全部条目 → 段落块列表（检索与排序的基本单位）。"""
    chunks: list[dict[str, str]] = []
    for entry in entries:
        chunks.extend(_chunk_entry(entry))
    return chunks


def _embedding_ready() -> bool:
    """向量检索是否可用（base_url 与 model 都配了才发网络；未配置即字符检索，零网络）。"""
    return bool(settings.EMBEDDING_BASE_URL.strip() and settings.EMBEDDING_MODEL.strip())


async def _embed_texts(texts: list[str]) -> list[list[float]]:
    """调本地 OpenAI 兼容 /embeddings 取向量（分批串行；任何异常上抛给调用方降级）。

    契约：base_url 以 /v1 结尾（与 LLM_PROVIDERS 同口径）；响应取 data[i].embedding，
    校验条数与维度一致——静默错位比报错更危险（会拿错向量排错序）。
    """
    url = f"{settings.EMBEDDING_BASE_URL.rstrip('/')}/embeddings"
    api_key = settings.EMBEDDING_API_KEY.get_secret_value()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=settings.EMBEDDING_TIMEOUT_SECONDS) as client:
        for start in range(0, len(texts), _EMBED_BATCH):
            batch = texts[start : start + _EMBED_BATCH]
            response = await client.post(
                url, json={"model": settings.EMBEDDING_MODEL, "input": batch}, headers=headers
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise ValueError(
                    f"embeddings 响应条数不符：期望 {len(batch)}，实际 "
                    f"{len(rows) if isinstance(rows, list) else '非法'}"
                )
            for row in rows:
                vector = row.get("embedding") if isinstance(row, dict) else None
                if not isinstance(vector, list) or not vector:
                    raise ValueError("embeddings 响应缺少 embedding 字段")
                vectors.append([float(value) for value in vector])
    return vectors


def _cosine(left: list[float], right: list[float]) -> float:
    """余弦相似度（维度不一致按 0 处理，不抛错——排序降级比整体失败更可用）。"""
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def _retrieve(
    query: str, chunks: list[dict[str, str]], top_k: int
) -> list[tuple[float, dict[str, str]]]:
    """字符 bigram 检索（回退通道）：命中率 = 命中的 query bigram 数 / 总数。"""
    query_grams = _bigrams(query)
    scored = [
        (score, chunk)
        for chunk in chunks
        if (score := _score_entry(query_grams, f"{chunk['title']}\n{chunk['text']}")) > 0
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:top_k]


async def _retrieve_by_embedding(
    query: str, chunks: list[dict[str, str]], top_k: int
) -> list[tuple[float, dict[str, str]]]:
    """向量语义检索：段落块与查询同批取向量后按余弦排序，低于门槛的不算命中。

    门槛（EMBEDDING_MIN_SCORE）不可省：余弦对任意文本都返回一个分数，不过滤就会
    把「问了库里没有的事」答成三条低分命中，等于把无命中伪装成有命中。
    """
    texts = [f"{chunk['title']}\n{chunk['text']}" for chunk in chunks]
    vectors = await _embed_texts([*texts, query])
    query_vector = vectors[-1]
    scored = [
        (round(_cosine(query_vector, vector), 4), chunk)
        for vector, chunk in zip(vectors[:-1], chunks, strict=True)
    ]
    scored = [pair for pair in scored if pair[0] >= settings.EMBEDDING_MIN_SCORE]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:top_k]


def _snippet(text: str, query_grams: set[str]) -> str:
    """截取命中片段：优先首个 bigram 命中位置，取前后窗口。"""
    lower = text.lower()
    pos = -1
    for gram in query_grams:
        pos = lower.find(gram)
        if pos >= 0:
            break
    if pos < 0:
        pos = 0
    start = max(0, pos - 20)
    return text[start : start + _SNIPPET_CHARS].replace("\n", " ").strip()


def _load_entries() -> tuple[list[dict[str, str]], bool]:
    """合并内置条目与 KB_DIR 本地文件；返回 (条目列表, 目录是否缺失)。"""
    entries = [
        {"title": item["title"], "content": item["content"], "source": "builtin-demo"}
        for item in _BUILTIN_ENTRIES
    ]
    root = Path(settings.KB_DIR)
    if not root.is_dir():
        return entries, True
    try:
        files = sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in _KB_SUFFIXES
        )
    except OSError as exc:
        logger.warning("知识库目录不可读（降级为内置条目）：%s", str(exc)[:120])
        return entries, True
    for path in files:
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                logger.warning("知识文件过大已跳过：%s", path.name)
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            logger.warning("知识文件读取失败已跳过 %s：%s", path.name, str(exc)[:120])
            continue
        if text:
            first_line = next((ln.strip("# \t") for ln in text.splitlines() if ln.strip()), "")
            entries.append(
                {
                    "title": first_line or path.stem,
                    "content": text,
                    "source": f"local-kb:{os.path.basename(path.name)}",
                }
            )
    return entries, False


async def _kb_ask(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """kb.ask：检索命中条目并摘录原文片段（不生成、不编造）。"""
    _ = ctx
    query = str(args.get("query") or "").strip()
    if not query:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 query 不能为空：请输入要咨询的问题")
    top_k = args.get("top_k", 3)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 10:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 top_k 必须是 1-10 的整数")

    entries, dir_missing = await asyncio.to_thread(_load_entries)
    chunks = _chunks_of(entries)
    mode = "bigram"
    fallback_reason = ""
    if _embedding_ready():
        try:
            scored = await _retrieve_by_embedding(query, chunks, top_k)
        except Exception as exc:  # 网络/超时/响应非法一律降级，绝不 500
            fallback_reason = (
                f"向量检索不可用已回退字符检索：{type(exc).__name__}: {str(exc)[:120]}"
            )
            logger.warning("kb.ask %s（model=%s）", fallback_reason, settings.EMBEDDING_MODEL)
            scored = _retrieve(query, chunks, top_k)
        else:
            mode = "embedding"
    else:
        scored = _retrieve(query, chunks, top_k)

    query_grams = _bigrams(query)
    results = [
        {
            "title": chunk["title"],
            # 向量命中时「相关区间 = 该段落」，整段返回比按字符取窗口更有意义；
            # 字符通道沿用原窗口截取口径（契约不变）。
            "snippet": (
                chunk["text"][:_CHUNK_CHARS]
                if mode == "embedding"
                else _snippet(chunk["text"], query_grams)
            ),
            "score": round(score, 4),
            "source": chunk["source"],
        }
        for score, chunk in scored
    ]
    return {
        "query": query,
        "results": results,
        "count": len(results),
        "retrieval_mode": mode,
        "embedding_model": settings.EMBEDDING_MODEL if mode == "embedding" else "",
        "retrieval_fallback_reason": fallback_reason,
        "degraded": not results,
        "degraded_reason": (
            "知识库中没有命中内容：请换个说法，或让管理员往知识目录补充资料" if not results else ""
        ),
        "kb_dir": settings.KB_DIR,
        "kb_dir_missing": dir_missing,
        "source": "local-kb",
        "fetched_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """kb.ask 的 ToolSpec。"""
    return (
        ToolSpec(
            name="kb.ask",
            scope=SCOPE_READ,
            description="企业知识库制度问答：检索内置条目与知识目录（KB_DIR，*.md/*.txt），按段落返回命中原文片段与来源溯源；配 EMBEDDING_* 时走向量语义检索（改说法也能命中），未配置或调用失败自动回退字符检索并在 retrieval_mode 如实标注；无命中时如实告知 degraded，绝不编造答案",
            params={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要咨询的问题（如：报销超 1000 元找谁审批）",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "top_k": {"type": "integer", "description": "返回命中条数（1-10，默认 3）"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_kb_ask,
        ),
    )


def register_all() -> list[str]:
    """注册知识库工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
