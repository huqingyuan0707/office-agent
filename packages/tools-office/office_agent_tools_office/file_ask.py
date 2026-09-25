"""文件内容问答工具（office.file.ask，对齐 PRD §2.1「文件解析：内容提取、解读与问答」）。

职责：
- office.file.ask（office:read）：对 DOCS_DIR 内指定文件（docx/xlsx/csv/txt/md/pdf）问一个
  具体问题（如「这份方案里验收标准是什么」），按段落检索最相关的片段并**只摘录原文**作答。
  检索内核复用 retrieval.py 双通道（配 EMBEDDING_* 走本地向量语义检索，未配置或调用失败
  回退字符 bigram），故「换一种说法」也能命中；相似度门槛滤掉明显低分片段（局限见下方边界）。
- 如实口径：文件缺 400/404、无可检索文本（扫描件 PDF 取不到文字）或零命中 → degraded +
  原因，不生成、不编造；解析引擎缺失（如 pypdf 未装）由 file_read.extract_document 如实降级。
- **已知边界（实测，勿当承诺）**：字符通道对「文件里没有的事」必然零命中；但向量通道的余弦
  对任意文本都给分，实测乱码问句对短文件仍得 0.37~0.39（阈值 0.35，见 retrieval 的校准说明），
  故向量通道下可能出现低分「命中」——分数在 `score` 原样暴露供调用方自判强弱，
  口径与 kb.ask 一致，未做按工具阈值的分叉（避免两处阈值漂移）。
- 与 kb.ask 的分工：kb.ask 面向知识目录 KB_DIR（多文件制度库），本工具面向**单份指定文件**
  （如某份合同/方案），二者共用检索原语但检索范围与入参契约不同。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler →
      file_read.extract_document（提取）→ retrieval（切块+检索）→ 摘录片段返回。
红线：纯本地实现，不触及 ORM / FastAPI；文件名锁 DOCS_DIR（paths.resolve_under_docs）。
对齐：AGENTS.md §3（分层红线 / 降级绝不 500 / 数据不出域）；
      智能办公Agent 产品需求文档.md §2.1（文件解析与问答）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings
from office_agent_tools_office.file_read import (
    MAX_FILE_BYTES,
    READ_SUFFIXES,
    extract_document,
)
from office_agent_tools_office.paths import resolve_under_docs
from office_agent_tools_office.retrieval import (
    chunks_of,
    embedding_ready,
    first_snippet,
    retrieve_bigram,
    retrieve_by_embedding,
)

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


async def _file_ask(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.file.ask：文件内段落检索问答（只摘录原文，不生成）。"""
    _ = ctx
    query = str(args.get("query") or "").strip()
    if not query:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 query 不能为空：请输入要问的问题")
    top_k = args.get("top_k", 3)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 10:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 top_k 必须是 1-10 的整数")

    path = resolve_under_docs(str(args.get("filename") or ""), READ_SUFFIXES)
    if not path.is_file():
        raise BusinessError(
            ErrorCode.NOT_FOUND, f"文件不存在：{path.name}（请先放入文档工作目录）", 404
        )
    if path.stat().st_size > MAX_FILE_BYTES:
        raise BusinessError(ErrorCode.PARAM_INVALID, "文件过大（超 200KB），请拆分后再提问")
    try:
        extracted = await asyncio.to_thread(extract_document, path)
    except (OSError, ValueError) as exc:
        logger.warning("文件问答解析失败 %s：%s", path.name, str(exc)[:120])
        raise BusinessError(ErrorCode.PARAM_INVALID, f"文件解析失败：{str(exc)[:120]}") from exc

    source = f"local-docs:{path.name}"
    text = str(extracted.get("text") or "").strip()
    if extracted.get("degraded") or not text:
        reason = str(extracted.get("degraded_reason") or "").strip() or (
            "文件内没有可检索文本（如纯扫描件 PDF 只有图片层）：请改用带文字层的文件"
        )
        return {
            "filename": path.name,
            "query": query,
            "answer_fragments": [],
            "count": 0,
            "retrieval_mode": "none",
            "embedding_model": "",
            "retrieval_fallback_reason": "",
            "degraded": True,
            "degraded_reason": reason,
            "source": source,
            "fetched_at": _now_text(),
        }

    # 粒度取「每段独立成块」：问「文件里 X 是什么」要能定位到具体段落，不做跨段合并
    chunks = chunks_of([{"title": path.name, "content": text, "source": source}], max_chars=None)
    mode = "bigram"
    fallback_reason = ""
    if embedding_ready():
        try:
            scored = await retrieve_by_embedding(query, chunks, top_k)
        except Exception as exc:  # 网络/超时/响应非法一律降级，绝不 500
            fallback_reason = (
                f"向量检索不可用已回退字符检索：{type(exc).__name__}: {str(exc)[:120]}"
            )
            logger.warning("file.ask %s（model=%s）", fallback_reason, settings.EMBEDDING_MODEL)
            scored = retrieve_bigram(query, chunks, top_k)
        else:
            mode = "embedding"
    else:
        scored = retrieve_bigram(query, chunks, top_k)

    fragments = [
        {
            # 向量通道返回命中整段；字符通道按窗口截取（口径由 retrieval.first_snippet 统一）
            "snippet": first_snippet(chunk["text"], query, mode),
            "score": round(score, 4),
            "source": chunk["source"],
        }
        for score, chunk in scored
    ]
    return {
        "filename": path.name,
        "format": path.suffix.lower().lstrip("."),
        "query": query,
        "answer_fragments": fragments,
        "count": len(fragments),
        "retrieval_mode": mode,
        "embedding_model": settings.EMBEDDING_MODEL if mode == "embedding" else "",
        "retrieval_fallback_reason": fallback_reason,
        "truncated": bool(extracted.get("truncated")),
        "degraded": not fragments,
        "degraded_reason": (
            f"文件《{path.name}》中没有与问题相关的段落：请换个说法，或确认问题所涉内容在文件内"
            if not fragments
            else ""
        ),
        "source": source,
        "fetched_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """office.file.ask 的 ToolSpec（只读，免审）。"""
    return (
        ToolSpec(
            name="office.file.ask",
            scope=SCOPE_READ,
            description="文件内容问答：对文档工作目录（DOCS_DIR）内指定文件（docx/xlsx/csv/txt/md/pdf）按段落检索并摘录与问题最相关的原文片段，回答「文件里 X 是什么」；配 EMBEDDING_* 时走向量语义检索（换说法也能命中），失败自动回退字符检索并如实标注 retrieval_mode；零命中/无文字层如实 degraded，绝不编造",
            params={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "文件名（不含路径，docx/xlsx/csv/txt/md/pdf）",
                        "minLength": 1,
                    },
                    "query": {
                        "type": "string",
                        "description": "针对该文件的问题（如：验收标准是什么）",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "top_k": {"type": "integer", "description": "返回命中片段数（1-10，默认 3）"},
                },
                "required": ["filename", "query"],
                "additionalProperties": False,
            },
            handler=_file_ask,
        ),
    )


def register_all() -> list[str]:
    """注册文件内容问答工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
