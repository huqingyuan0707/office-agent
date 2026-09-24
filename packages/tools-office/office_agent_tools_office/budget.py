"""预算查询工具（office.budget.query，对齐 PRD §2.10 / §5.3 V1.2「预算查询」）。

职责：
- office.budget.query（office:read）：查项目预算台账（内置演示数据 + DOCS_DIR/data/budget.csv
  可选叠加），按项目名包含过滤；每行 remaining=budget-used、usage_pct=used/budget 为
  确定性计算（口径在响应注明），并给 充足/紧张/超支 状态标签（规则：remaining<0 超支、
  usage_pct>=90 紧张、其余充足）；匹配行汇总 total_budget/total_used/total_remaining。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：读口径（免审批）；纯本地实现，不触及 ORM / FastAPI；CSV 叠加读失败只降级不阻断；
      响应带 source + fetched_at 溯源——真实部署对接外部台账时一律经白名单工具拉取
      （数据不出域），本工具不直连任何外部库。
对齐：AGENTS.md §3（降级绝不 500/溯源）；智能办公Agent 产品需求文档.md §2.10
      （预算剩余额度查询——演示台账口径，外部对接走 linkage 白名单）。
"""

from __future__ import annotations

import asyncio
import copy
import math
from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from .data_analysis import _load_csv_overlay

SCOPE_READ = "office:read"

_MAX_LIMIT = 100

#: 内置演示预算台账（source=builtin-demo；真实部署用 DOCS_DIR/data/budget.csv 叠加/替换行）
_BUDGET_ROWS: list[dict[str, Any]] = [
    {"project": "官网改版", "department": "产品部", "year": 2026, "budget": 200000, "used": 126000},
    {
        "project": "移动端适配",
        "department": "研发部",
        "year": 2026,
        "budget": 150000,
        "used": 30000,
    },
    {"project": "数据看板", "department": "研发部", "year": 2026, "budget": 80000, "used": 82000},
    {"project": "客服知识库", "department": "产品部", "year": 2026, "budget": 60000, "used": 57000},
    {"project": "品牌物料", "department": "市场部", "year": 2026, "budget": 120000, "used": 45000},
]


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _to_number(value: Any) -> float | None:
    """台账金额取值：数字直取（bool 拒绝），数字字符串可转，其余 None（不编造）。"""
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def _status(remaining: float | None, usage_pct: float | None) -> str:
    """预算状态标签（确定性规则：超支/紧张/充足；缺数如实「未知」）。"""
    if remaining is not None and remaining < 0:
        return "超支"
    if usage_pct is not None and usage_pct >= 90:
        return "紧张"
    if remaining is None or usage_pct is None:
        return "未知"
    return "充足"


def _enrich(row: dict[str, Any]) -> dict[str, Any]:
    """补确定性派生值：remaining / usage_pct / status（口径注明，非编造）。"""
    budget = _to_number(row.get("budget"))
    used = _to_number(row.get("used"))
    remaining = budget - used if budget is not None and used is not None else None
    usage_pct = (
        round(used / budget * 100, 2)
        if budget is not None and used is not None and budget > 0
        else None
    )
    enriched = dict(row)
    enriched["remaining"] = remaining
    enriched["usage_pct"] = usage_pct
    enriched["status"] = _status(remaining, usage_pct)
    return enriched


async def _budget_query(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.budget.query：查预算台账（演示数据 + CSV 叠加），派生剩余额度与状态。"""
    _ = ctx
    project = str(args.get("project") or "").strip()
    limit = args.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 limit 必须是 1-{_MAX_LIMIT} 的整数")

    base = copy.deepcopy(_BUDGET_ROWS)
    overlay, applied = await asyncio.to_thread(_load_csv_overlay, "budget")
    all_rows = base + overlay
    matched = [
        _enrich(row) for row in all_rows if not project or project in str(row.get("project") or "")
    ][:limit]

    budgets = [_to_number(row.get("budget")) for row in matched]
    useds = [_to_number(row.get("used")) for row in matched]
    total_budget = math.fsum(value for value in budgets if value is not None)
    total_used = math.fsum(value for value in useds if value is not None)
    return {
        "project": project,
        "rows": matched,
        "count": len(matched),
        "total": len(all_rows),
        "summary": {
            "total_budget": total_budget,
            "total_used": total_used,
            "total_remaining": total_budget - total_used,
        },
        "formula": "remaining=budget-used；usage_pct=used/budget*100（保留 2 位，预算 0 不算比）",
        "overlay_applied": applied,
        "source": "builtin-demo" + ("+local-csv" if applied else ""),
        "fetched_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """预算查询工具的 ToolSpec（读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.budget.query",
            scope=SCOPE_READ,
            description="查询项目预算台账：预算/已用/剩余额度与使用率（确定性计算并注明口径），"
            "附 充足/紧张/超支 状态标签与汇总；内置演示数据 + DOCS_DIR/data/budget.csv 可选叠加，"
            "带 source + fetched_at 溯源",
            params={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "项目名过滤（包含匹配，缺省返回全部）",
                        "maxLength": 60,
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"返回行数上限（1-{_MAX_LIMIT}，默认 20）",
                    },
                },
                "additionalProperties": False,
            },
            handler=_budget_query,
        ),
    )


def register_all() -> list[str]:
    """注册预算查询工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
