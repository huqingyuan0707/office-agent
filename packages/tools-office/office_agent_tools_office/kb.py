"""企业知识库问答工具（kb.ask：本地条目检索，制度答疑最小闭环）。

职责：
- kb.ask（office:read）：对「内置演示条目 + KB_DIR 本地文件（*.md/*.txt）」做关键词检索，
  返回 top_k 命中片段，带 source + fetched_at 溯源；库为空时 degraded=True 留白不编造答案。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：纯本地实现，不触及 ORM / FastAPI；答案只摘录命中条目原文（不生成、不编造）；
      检索失败（目录不可读）走降级路径返回 degraded，绝不 500。
对齐：AGENTS.md §3（分层红线）；智能办公Agent 产品需求文档.md §5.1（V1.0 知识库问答）、
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

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

_KB_SUFFIXES = (".md", ".txt")
_SNIPPET_CHARS = 120
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
    query_grams = _bigrams(query)
    scored = []
    for entry in entries:
        score = _score_entry(query_grams, f"{entry['title']}\n{entry['content']}")
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    results = [
        {
            "title": entry["title"],
            "snippet": _snippet(entry["content"], query_grams),
            "score": round(score, 4),
            "source": entry["source"],
        }
        for score, entry in scored[:top_k]
    ]
    return {
        "query": query,
        "results": results,
        "count": len(results),
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
            description="企业知识库制度问答：检索内置条目与知识目录（KB_DIR，*.md/*.txt），返回命中原文片段与来源溯源；无命中时如实告知 degraded，绝不编造答案",
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
