"""项目台账查询（office.project.query，对齐 PRD §2.9「项目台账查询」）。

职责（office:read，免审）：
- 查项目台账（内置演示 + DOCS_DIR/data/projects.csv 可选叠加）：每行含里程碑
  （[{name, due?, status}]）/ 风险（[{desc, level}]）/ 责任人 / 进度；
- 过滤：name 包含 / owner 等值 / status 等值 / has_risk（只看有风险项）；
- 出参：命中项目（含当前里程碑 = 首个非 done 里程碑）+ 组合计（总数/分状态/
  高风险项目数）+ 中文项目简报 brief（数值全部原值直出）。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：CSV 叠加只合流顶层标量列（里程碑/风险嵌套只认内置行，CSV 行缺失即空数组，
      不臆造结构）；风险等级只收 low/medium/high，其余原样透出不改写；绝不 500。
对齐：AGENTS.md §3（降级不 500/溯源/不编造）；智能办公Agent 产品需求文档.md §2.9。
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

from .data_analysis import _load_csv_overlay

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

_MAX_LIMIT = 50

#: 内置演示项目台账（source=builtin-demo；真实部署用 DOCS_DIR/data/projects.csv 叠加行）
_PROJECT_ROWS: list[dict[str, Any]] = [
    {
        "name": "官网改版",
        "owner": "产品经理",
        "status": "进行中",
        "progress": 60,
        "milestones": [
            {"name": "需求定稿", "due": "2026-08-30", "status": "done"},
            {"name": "上线发布", "due": "2026-10-15", "status": "doing"},
        ],
        "risks": [{"desc": "UI 稿确认滞后", "level": "medium"}],
    },
    {
        "name": "移动端适配",
        "owner": "前端工程师",
        "status": "未开始",
        "progress": 0,
        "milestones": [{"name": "技术选型", "due": "2026-10-01", "status": "todo"}],
        "risks": [],
    },
    {
        "name": "数据看板",
        "owner": "后端工程师",
        "status": "已完成",
        "progress": 100,
        "milestones": [{"name": "交付验收", "due": "2026-09-10", "status": "done"}],
        "risks": [],
    },
]


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _milestones_of(row: dict[str, Any]) -> list[dict[str, str]]:
    """里程碑口径：只收对象数组（CSV 行无嵌套即空数组，不臆造）。"""
    raw = row.get("milestones")
    if not isinstance(raw, list):
        return []
    return [
        {
            "name": str(m.get("name") or ""),
            "due": str(m.get("due") or ""),
            "status": str(m.get("status") or ""),
        }
        for m in raw
        if isinstance(m, dict) and str(m.get("name") or "")
    ]


def _risks_of(row: dict[str, Any]) -> list[dict[str, str]]:
    """风险口径：只收对象数组；level 原样透出（low/medium/high 外的值不改写）。"""
    raw = row.get("risks")
    if not isinstance(raw, list):
        return []
    return [
        {"desc": str(r.get("desc") or ""), "level": str(r.get("level") or "")}
        for r in raw
        if isinstance(r, dict) and str(r.get("desc") or "")
    ]


def _current_milestone(milestones: list[dict[str, str]]) -> str:
    """当前里程碑 = 首个非 done 项；全 done/空即「—」（不编造下一项）。"""
    for item in milestones:
        if item["status"] != "done":
            return f"{item['name']}（{item['status']}）"
    return "—"


async def _project_query(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.project.query：项目台账查询（里程碑/风险/责任人 + 简报）。"""
    _ = ctx
    name = str(args.get("name") or "").strip()
    owner = str(args.get("owner") or "").strip()
    status = str(args.get("status") or "").strip()
    has_risk = args.get("has_risk", False)
    if not isinstance(has_risk, bool):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 has_risk 必须是布尔值")
    limit = args.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 limit 必须是 1-{_MAX_LIMIT} 的整数")
    overlay, applied = await asyncio.to_thread(_load_csv_overlay, "projects")
    items = []
    for row in copy.deepcopy(_PROJECT_ROWS) + overlay:
        if name and name not in str(row.get("name") or ""):
            continue
        if owner and str(row.get("owner") or "") != owner:
            continue
        if status and str(row.get("status") or "") != status:
            continue
        milestones = _milestones_of(row)
        risks = _risks_of(row)
        if has_risk and not risks:
            continue
        items.append(
            {
                "name": str(row.get("name") or ""),
                "owner": str(row.get("owner") or ""),
                "status": str(row.get("status") or ""),
                "progress": row.get("progress"),
                "current_milestone": _current_milestone(milestones),
                "milestones": milestones,
                "risks": risks,
            }
        )
    matched = items[:limit]
    by_status: dict[str, int] = {}
    high_risk = 0
    for item in matched:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        if any(r["level"] == "high" for r in item["risks"]):
            high_risk += 1
    lines = [f"# 项目简报：命中 {len(matched)} 个项目"]
    for item in matched:
        risk_text = "、".join(f"{r['desc']}（{r['level']}）" for r in item["risks"]) or "无记录风险"
        lines.append(
            f"- {item['name']}：{item['status']}（进度{item['progress']}%），"
            f"责任人{item['owner']}，当前里程碑{item['current_milestone']}，风险：{risk_text}"
        )
    source = "builtin-demo" + ("+local-csv" if applied else "")
    return {
        "projects": matched,
        "count": len(matched),
        "by_status": by_status,
        "high_risk_count": high_risk,
        "brief": "\n".join(lines),
        "source": source,
        "fetched_at": _now_text(),
    }


def specs() -> tuple[ToolSpec, ...]:
    """项目台账工具的 ToolSpec（读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.project.query",
            scope=SCOPE_READ,
            description="项目台账查询：里程碑/风险/责任人/进度（内置 + projects.csv 叠加），"
            "支持按名包含/责任人/状态/有无风险过滤，附中文项目简报",
            params={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "项目名包含过滤"},
                    "owner": {"type": "string", "description": "责任人等值过滤"},
                    "status": {"type": "string", "description": "状态等值过滤"},
                    "has_risk": {"type": "boolean", "description": "只看有风险项的项目"},
                    "limit": {"type": "integer", "description": "返回上限（1-50，默认 20）"},
                },
                "additionalProperties": False,
            },
            handler=_project_query,
        ),
    )


def register_all() -> list[str]:
    """注册项目台账工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
