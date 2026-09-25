"""财务简易辅助（office.finance.reimburse / expense，对齐 PRD §2.10 前两项）。

职责（全读口径，免审）：
- office.finance.reimburse：个人报销进度查询——内置报销台账（单号/事由/金额/
  stage/更新时间）按 person 等值 + stage 等值过滤，附各阶段计数与在途金额
  （pending + approved 未打款口径注明）；
- office.finance.expense：部门费用台账简易统计——内置费用台账（部门/项目/
  类目/金额/月份）按 department + month 前缀过滤，按类目汇总 + 总额/笔数；
  project 命中 budget 台账时联带 remaining/usage_pct（复用 budget 确定性口径，
  预算未覆盖即 budget=None 不臆造额度）。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：CSV 叠加（reimbursements.csv / expenses.csv）读失败只降级；金额只取原值，
      数字字符串可转（budget 同口径），转不出即整行跳过并计数（不编造补数）；
      绝不 500。
对齐：AGENTS.md §3（降级不 500/溯源/不编造）；智能办公Agent 产品需求文档.md §2.10。
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import UTC, datetime
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register

from .budget import _BUDGET_ROWS
from .data_analysis import _load_csv_overlay

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

_MAX_LIMIT = 100

#: 内置演示报销台账（source=builtin-demo；真实部署对接财务库走白名单工具）
_REIMBURSE_ROWS: list[dict[str, Any]] = [
    {
        "no": "BX-2026-091",
        "person": "张三",
        "title": "差旅费",
        "amount": 3200.5,
        "stage": "审批中",
        "updated": "2026-09-24",
    },
    {
        "no": "BX-2026-088",
        "person": "张三",
        "title": "招待费",
        "amount": 1500,
        "stage": "已打款",
        "updated": "2026-09-20",
    },
    {
        "no": "BX-2026-092",
        "person": "李四",
        "title": "办公用品",
        "amount": 680,
        "stage": "已驳回",
        "updated": "2026-09-23",
    },
    {
        "no": "BX-2026-095",
        "person": "李四",
        "title": "培训费",
        "amount": 4200,
        "stage": "已通过",
        "updated": "2026-09-25",
    },
]

#: 内置演示费用台账（部门/项目/类目/金额/月份；project 可联带 budget 剩余额度）
_EXPENSE_ROWS: list[dict[str, Any]] = [
    {
        "department": "产品部",
        "project": "官网改版",
        "category": "人力",
        "amount": 80000,
        "month": "2026-09",
    },
    {
        "department": "产品部",
        "project": "官网改版",
        "category": "采购",
        "amount": 12000,
        "month": "2026-09",
    },
    {
        "department": "研发部",
        "project": "数据看板",
        "category": "人力",
        "amount": 60000,
        "month": "2026-09",
    },
    {
        "department": "研发部",
        "project": "数据看板",
        "category": "云服务",
        "amount": 8000,
        "month": "2026-08",
    },
]


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _to_amount(value: Any) -> float | None:
    """金额取值（budget 同口径：bool 拒绝，数字字符串可转，其余 None）。"""
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


async def _finance_reimburse(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.finance.reimburse：个人报销进度（单号/事由/金额/节点 + 阶段计数）。"""
    _ = ctx
    person = str(args.get("person") or "").strip()
    if not person:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 person 不能为空：请传入报销人姓名")
    stage = str(args.get("stage") or "").strip()
    overlay, applied = await asyncio.to_thread(_load_csv_overlay, "reimbursements")
    records = []
    skipped = 0
    for row in copy.deepcopy(_REIMBURSE_ROWS) + overlay:
        if str(row.get("person") or "") != person:
            continue
        if stage and str(row.get("stage") or "") != stage:
            continue
        amount = _to_amount(row.get("amount"))
        if amount is None:
            skipped += 1
            continue
        records.append(
            {
                "no": str(row.get("no") or ""),
                "title": str(row.get("title") or ""),
                "amount": amount,
                "stage": str(row.get("stage") or ""),
                "updated": str(row.get("updated") or ""),
            }
        )
    by_stage: dict[str, int] = {}
    transit = 0.0
    for rec in records:
        by_stage[rec["stage"]] = by_stage.get(rec["stage"], 0) + 1
        if rec["stage"] in ("审批中", "已通过"):
            transit += rec["amount"]
    source = "builtin-demo" + ("+local-csv" if applied else "")
    return {
        "person": person,
        "records": records,
        "count": len(records),
        "by_stage": by_stage,
        "transit_amount": round(transit, 2),
        "transit_rule": "在途金额=审批中+已通过（未打款）之和",
        "skipped": skipped,
        "source": source,
        "fetched_at": _now_text(),
    }


def _budget_for(project: str) -> dict[str, Any] | None:
    """预算联带：project 命中 budget 台账即返回剩余额度（budget 确定性口径），否则 None。"""
    for row in _BUDGET_ROWS:
        if str(row.get("project") or "") != project:
            continue
        budget = _to_amount(row.get("budget"))
        used = _to_amount(row.get("used"))
        if budget is None or used is None:
            return None
        remaining = budget - used
        return {
            "budget": budget,
            "used": used,
            "remaining": remaining,
            "usage_pct": round(used / budget * 100, 2) if budget != 0 else None,
        }
    return None


async def _finance_expense(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.finance.expense：部门费用简易统计（总额/笔数/按类目 + 预算联带）。"""
    _ = ctx
    department = str(args.get("department") or "").strip()
    if not department:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 department 不能为空：请传入部门名")
    month = str(args.get("month") or "").strip()
    overlay, applied = await asyncio.to_thread(_load_csv_overlay, "expenses")
    total = 0.0
    count = 0
    skipped = 0
    by_category: dict[str, float] = {}
    projects: set[str] = set()
    for row in copy.deepcopy(_EXPENSE_ROWS) + overlay:
        if str(row.get("department") or "") != department:
            continue
        if month and not str(row.get("month") or "").startswith(month):
            continue
        amount = _to_amount(row.get("amount"))
        if amount is None:
            skipped += 1
            continue
        total += amount
        count += 1
        category = str(row.get("category") or "未分类")
        by_category[category] = round(by_category.get(category, 0.0) + amount, 2)
        if row.get("project"):
            projects.add(str(row["project"]))
    budgets = {name: info for name in sorted(projects) if (info := _budget_for(name)) is not None}
    source = "builtin-demo" + ("+local-csv" if applied else "")
    return {
        "department": department,
        "month": month or "全部",
        "total": round(total, 2),
        "count": count,
        "by_category": by_category,
        "budgets": budgets,
        "skipped": skipped,
        "source": source,
        "fetched_at": _now_text(),
        "note": "预算联带只覆盖命中 budget 台账的项目，未覆盖即缺席不臆造额度",
    }


def specs() -> tuple[ToolSpec, ...]:
    """财务两工具的 ToolSpec（全读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.finance.reimburse",
            scope=SCOPE_READ,
            description="个人报销进度查询：按人（可按节点过滤）列单号/事由/金额/当前节点，"
            "附各阶段计数与在途金额（审批中+已通过未打款）",
            params={
                "type": "object",
                "properties": {
                    "person": {"type": "string", "description": "报销人姓名", "minLength": 1},
                    "stage": {
                        "type": "string",
                        "description": "节点过滤（审批中/已通过/已打款/已驳回）",
                    },
                },
                "required": ["person"],
                "additionalProperties": False,
            },
            handler=_finance_reimburse,
        ),
        ToolSpec(
            name="office.finance.expense",
            scope=SCOPE_READ,
            description="部门费用台账简易统计：按部门（可按月份前缀过滤）汇总总额/笔数/按类目，"
            "项目命中预算台账时联带剩余额度与使用率",
            params={
                "type": "object",
                "properties": {
                    "department": {"type": "string", "description": "部门名", "minLength": 1},
                    "month": {"type": "string", "description": "月份前缀（例 2026-09，缺省全部）"},
                },
                "required": ["department"],
                "additionalProperties": False,
            },
            handler=_finance_expense,
        ),
    )


def register_all() -> list[str]:
    """注册财务两工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
