"""企业知识库问答工具（kb.ask：双通道检索，制度答疑最小闭环）。

职责：
- kb.ask（office:read）：对「内置演示条目 + KB_DIR 本地文件（*.md/*.txt）」做检索，
  返回 top_k 命中片段，带 source + fetched_at 溯源；库为空时 degraded=True 留白不编造答案。
- 检索双通道：**向量语义检索**优先（Settings 的 EMBEDDING_* 配置本地 OpenAI 兼容
  /embeddings，按段落余弦相似度排序）→ 未配置或调用失败时回退**字符 bigram 检索**。
  降级只在结果里如实标注（retrieval_mode / retrieval_fallback_reason），绝不 500。
- 检索原语（切块 / 双通道 / 片段口径）已抽到同包 retrieval.py，与 office.file.ask 共用。

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
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings
from office_agent_tools_office.retrieval import (
    chunks_of,
    embedding_ready,
    first_snippet,
    retrieve_bigram,
    retrieve_by_embedding,
)

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

_KB_SUFFIXES = (".md", ".txt")
_MAX_FILE_BYTES = 200_000  # 单文件读取上限（防超大文件拖垮响应）

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
    chunks = chunks_of(entries)
    mode = "bigram"
    fallback_reason = ""
    if embedding_ready():
        try:
            scored = await retrieve_by_embedding(query, chunks, top_k)
        except Exception as exc:  # 网络/超时/响应非法一律降级，绝不 500
            fallback_reason = (
                f"向量检索不可用已回退字符检索：{type(exc).__name__}: {str(exc)[:120]}"
            )
            logger.warning("kb.ask %s（model=%s）", fallback_reason, settings.EMBEDDING_MODEL)
            scored = retrieve_bigram(query, chunks, top_k)
        else:
            mode = "embedding"
    else:
        scored = retrieve_bigram(query, chunks, top_k)

    results = [
        {
            "title": chunk["title"],
            "snippet": first_snippet(chunk["text"], query, mode),
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
