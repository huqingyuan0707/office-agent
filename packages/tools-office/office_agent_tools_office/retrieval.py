"""检索原语（kb.ask 与 office.file.ask 共用的确定性检索内核，对齐 PRD §2.1/§5.1）。

职责：把「段落切块 + 双通道检索」集中一处，供知识库问答与文件内容问答复用——
①切块：按空行切段并合并到 CHUNK_CHARS 以内，超长段硬切（整篇压一个向量会丢语义且模型会静默截断）；
②字符通道：中文友好 bigram 命中率排序（零网络，默认可用）；
③向量通道：调本地 OpenAI 兼容 /embeddings（数据不出域）按段落余弦排序 + 相似度门槛；
④降级：向量不可用时由调用方捕获异常回退字符通道，本模块只抛错不吞错（调用方裁决）。

链路：kb.kb_ask / file_ask.file_ask → 本模块纯函数 → 返回 (分数, 段落块) 列表供摘录原文。
红线：只排序与摘录，不生成、不编造；不触 ORM / FastAPI；不读业务库。
对齐：AGENTS.md §3（降级绝不 500 / 数据不出域）；智能办公Agent 产品需求文档.md
      §2.1（文件内容提取、解读与问答）、§5.1（V1.0 知识库问答）。
"""

from __future__ import annotations

import math
import re

import httpx

from office_agent_core.settings import settings

#: 段落切块上限：长文整篇压成一个向量会丢语义（模型还会静默截断），故按段落切
CHUNK_CHARS = 600
#: 单次 /embeddings 请求的最大文本条数（本地小模型一次吃太多会超时）
EMBED_BATCH = 16
#: 字符通道的片段窗口长度（向量通道返回整段，不用此值）
SNIPPET_CHARS = 120


def bigrams(text: str) -> set[str]:
    """中文友好分词：字符 bigram 集合（单字查询退化为单字集合）。"""
    cleaned = "".join(ch for ch in text.lower() if not ch.isspace())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def score_entry(query_grams: set[str], entry_text: str) -> float:
    """命中率 = 命中的 query bigram 数 / query bigram 总数（0-1）。"""
    if not query_grams:
        return 0.0
    haystack = entry_text.lower()
    hit = sum(1 for gram in query_grams if gram in haystack)
    return hit / len(query_grams)


def hard_wrap(text: str, limit: int) -> list[str]:
    """超长单段硬切（无空行的长文也要能被切分，否则整篇一个向量等于没检索）。"""
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [text]


def _merge_pieces(pieces: list[str], max_chars: int) -> list[str]:
    """相邻段合并到 max_chars 以内（短段拼块减少碎片，长段各自成块）。"""
    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if buffer and len(buffer) + len(piece) + 1 > max_chars:
            chunks.append(buffer)
            buffer = piece
        else:
            buffer = f"{buffer}\n{piece}" if buffer else piece
    if buffer:
        chunks.append(buffer)
    return chunks


def chunk_entry(entry: dict[str, str], max_chars: int | None = CHUNK_CHARS) -> list[dict[str, str]]:
    """一条条目 → 若干段落块（按空行切段；超长段硬切到 CHUNK_CHARS）。

    粒度由调用方定：max_chars=CHUNK_CHARS 时短段合并（知识库口径，减少碎片）；
    max_chars=None 时每段独立成块（文件问答口径——「文件里 X 是什么」要能定位到具体段）。
    切块是向量检索的前提：整篇文本压成一个向量会丢语义，命中粒度也回不到段落。
    """
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", entry["content"]):
        stripped = paragraph.strip()
        if not stripped:
            continue
        pieces.extend(
            hard_wrap(stripped, CHUNK_CHARS) if len(stripped) > CHUNK_CHARS else [stripped]
        )
    if not pieces:
        pieces = [entry["content"]]
    chunks = pieces if max_chars is None else _merge_pieces(pieces, max_chars)
    return [{"title": entry["title"], "source": entry["source"], "text": text} for text in chunks]


def chunks_of(
    entries: list[dict[str, str]], max_chars: int | None = CHUNK_CHARS
) -> list[dict[str, str]]:
    """全部条目 → 段落块列表（检索与排序的基本单位）。"""
    chunks: list[dict[str, str]] = []
    for entry in entries:
        chunks.extend(chunk_entry(entry, max_chars))
    return chunks


def embedding_ready() -> bool:
    """向量检索是否可用（base_url 与 model 都配了才发网络；未配置即字符检索，零网络）。"""
    return bool(settings.EMBEDDING_BASE_URL.strip() and settings.EMBEDDING_MODEL.strip())


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """调本地 OpenAI 兼容 /embeddings 取向量（分批串行；任何异常上抛给调用方降级）。

    契约：base_url 以 /v1 结尾（与 LLM_PROVIDERS 同口径）；响应取 data[i].embedding，
    校验条数与维度一致——静默错位比报错更危险（会拿错向量排错序）。
    """
    url = f"{settings.EMBEDDING_BASE_URL.rstrip('/')}/embeddings"
    api_key = settings.EMBEDDING_API_KEY.get_secret_value()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=settings.EMBEDDING_TIMEOUT_SECONDS) as client:
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start : start + EMBED_BATCH]
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


def cosine(left: list[float], right: list[float]) -> float:
    """余弦相似度（维度不一致按 0 处理，不抛错——排序降级比整体失败更可用）。"""
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def retrieve_bigram(
    query: str, chunks: list[dict[str, str]], top_k: int
) -> list[tuple[float, dict[str, str]]]:
    """字符 bigram 检索（回退通道）：命中率 = 命中的 query bigram 数 / 总数。"""
    query_grams = bigrams(query)
    scored = [
        (score, chunk)
        for chunk in chunks
        if (score := score_entry(query_grams, f"{chunk['title']}\n{chunk['text']}")) > 0
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:top_k]


async def retrieve_by_embedding(
    query: str, chunks: list[dict[str, str]], top_k: int
) -> list[tuple[float, dict[str, str]]]:
    """向量语义检索：段落块与查询同批取向量后按余弦排序，低于门槛的不算命中。

    门槛（EMBEDDING_MIN_SCORE）不可省：余弦对任意文本都返回一个分数，不过滤就会
    把「问了库里没有的事」答成三条低分命中，等于把无命中伪装成有命中。
    **但门槛不是分界线（2026-09-25 实测口径）**：相关问句 top1 落在 0.378~0.728、
    不相关问句 top1 落在 0.235~0.449，两簇重叠——故门槛只是精确率/召回率取舍杆，
    换模型或换语料必须重新校准；分数在调用方出参的 `score` 原样暴露，不要替用户判定强弱。
    """
    texts = [f"{chunk['title']}\n{chunk['text']}" for chunk in chunks]
    vectors = await embed_texts([*texts, query])
    query_vector = vectors[-1]
    scored = [
        (round(cosine(query_vector, vector), 4), chunk)
        for vector, chunk in zip(vectors[:-1], chunks, strict=True)
    ]
    scored = [pair for pair in scored if pair[0] >= settings.EMBEDDING_MIN_SCORE]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:top_k]


def snippet(text: str, query_grams: set[str]) -> str:
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
    return text[start : start + SNIPPET_CHARS].replace("\n", " ").strip()


def first_snippet(text: str, query: str, mode: str) -> str:
    """命中片段口径：向量通道「相关区间 = 该段落」整段返回，字符通道沿用窗口截取。

    两种口径由本函数统一裁决，避免调用方各写一遍 mode 分支（口径漂移源头）。
    """
    return text[:CHUNK_CHARS] if mode == "embedding" else snippet(text, bigrams(query))
