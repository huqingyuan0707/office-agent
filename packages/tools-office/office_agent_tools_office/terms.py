"""内部术语库翻译工具（office.terms.translate，对齐 PRD §2.1 术语翻译）。

职责：office.terms.translate（office:read）——内置办公术语表 + DOCS_DIR/terms.csv
      可选叠加，按源词长度降序做确定性替换（专业名词统一）；返回替换明细与计数，
      零命中原样返回并明示（不编造译文）。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：纯本地实现，不触及 ORM / FastAPI；词典式替换（无 LLM 不做整句机翻）；
      CSV 叠加缺席/损坏只降级不阻断，绝不 500。
对齐：AGENTS.md §3（分层/降级绝不 500）；智能办公Agent 产品需求文档.md §2.1
      （新增：内部术语库翻译，专业名词统一，支持多语种文档翻译——整句机翻需大模型，
      明确后置，本工具只做术语级统一替换）。
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from pathlib import Path
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

#: 内置办公术语表（source → target 双向共存；中英混排文本一次过全统一）
_BUILTIN_TERMS: tuple[tuple[str, str], ...] = (
    ("会议纪要", "meeting minutes"),
    ("meeting minutes", "会议纪要"),
    ("weekly report", "周报"),
    ("daily report", "日报"),
    ("monthly report", "月报"),
    ("knowledge base", "知识库"),
    ("circuit breaker", "熔断"),
    ("idempotency", "幂等"),
    ("reimbursement", "报销"),
    ("approval", "审批"),
    ("workflow", "工作流"),
    ("provenance", "溯源"),
    ("todo", "待办"),
    ("日报", "daily report"),
    ("周报", "weekly report"),
    ("月报", "monthly report"),
    ("知识库", "knowledge base"),
    ("熔断", "circuit breaker"),
    ("幂等", "idempotency"),
    ("报销", "reimbursement"),
    ("审批", "approval"),
    ("工作流", "workflow"),
    ("溯源", "provenance"),
    ("待办", "todo"),
)


def _load_csv_terms() -> tuple[list[tuple[str, str]], bool]:
    """读 DOCS_DIR/terms.csv 叠加术语（source,target 两列；首行表头自动跳过）。"""
    path = Path(settings.DOCS_DIR) / "terms.csv"
    if not path.is_file():
        return [], False
    try:
        if path.stat().st_size > 200_000:
            logger.warning("术语表过大已跳过：%s", path.name)
            return [], False
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        rows = [row for row in csv.reader(io.StringIO(text)) if row]
    except (OSError, csv.Error) as exc:
        logger.warning("术语表读取失败已跳过 %s：%s", path.name, str(exc)[:120])
        return [], False
    terms: list[tuple[str, str]] = []
    for row in rows:
        if len(row) < 2:
            continue
        source, target = row[0].strip(), row[1].strip()
        if not source or not target:
            continue
        if source.lower() == "source" and target.lower() == "target":
            continue
        terms.append((source, target))
    return terms, True


async def _terms_translate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.terms.translate：术语统一替换（最长源词优先，逐项计数）。"""
    _ = ctx
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 text 不能为空：请传入待统一术语的文本")
    overlay, applied = await asyncio.to_thread(_load_csv_terms)
    # 单遍替换（长源词优先的正则交替，一次扫描）：避免“日报→daily report”刚换上又被
    # 反向对换回去的乒乓计数；CSV 同源词覆盖内置表（后写优先）。
    mapping: dict[str, str] = dict(_BUILTIN_TERMS)
    mapping.update(dict(overlay))
    ordered = sorted(mapping, key=len, reverse=True)
    counts: dict[str, int] = {}

    def _sub(match: re.Match[str]) -> str:
        word = match.group(0)
        counts[word] = counts.get(word, 0) + 1
        return mapping[word]

    current = re.compile("|".join(re.escape(word) for word in ordered)).sub(_sub, text)
    replacements = [
        {"source": word, "target": mapping[word], "count": counts[word]}
        for word in ordered
        if word in counts
    ]
    return {
        "translated": current,
        "replacements": replacements,
        "replacement_count": sum(item["count"] for item in replacements),
        "term_count": len(mapping),
        "overlay_applied": applied,
        "note": (
            "术语级统一替换（词典式，非整句机翻）；零命中原样返回"
            if not replacements
            else "术语级统一替换（词典式，非整句机翻）"
        ),
        "source": "builtin-terms" + ("+local-csv" if applied else ""),
    }


def specs() -> tuple[ToolSpec, ...]:
    """术语翻译工具的 ToolSpec（读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.terms.translate",
            scope=SCOPE_READ,
            description="内部术语库翻译：内置办公术语表 + DOCS_DIR/terms.csv 叠加，最长匹配确定性替换（中英双向一次过）；零命中原样返回不编造",
            params={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "待统一术语的文本", "minLength": 1},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=_terms_translate,
        ),
    )


def register_all() -> list[str]:
    """注册术语翻译工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
