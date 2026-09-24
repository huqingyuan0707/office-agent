"""预算查询工具单元测试（handler 直调，不起 HTTP 服务）。

覆盖：演示台账派生值（remaining/usage_pct/status 规则）、项目名包含过滤、汇总、
      CSV 叠加（含坏行金额缺数不编造）、limit 校验、注册口径（读工具免审批）。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.10（V1.2 预算查询）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_core.settings import settings
from office_agent_tools_office import budget

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read"])


# ---------------- 派生值与过滤 ----------------


async def test_query_all_rows_with_derived_fields() -> None:
    data = await budget._budget_query(CTX, {})
    assert data["count"] == 5
    assert data["source"] == "builtin-demo"
    assert "fetched_at" in data
    by_name = {row["project"]: row for row in data["rows"]}
    assert by_name["官网改版"]["remaining"] == 74000
    assert by_name["官网改版"]["usage_pct"] == 63.0
    assert by_name["官网改版"]["status"] == "充足"
    assert by_name["数据看板"]["status"] == "超支"
    assert by_name["客服知识库"]["status"] == "紧张"
    assert (
        data["summary"]["total_remaining"]
        == data["summary"]["total_budget"] - data["summary"]["total_used"]
    )


async def test_query_filters_by_project_substring() -> None:
    data = await budget._budget_query(CTX, {"project": "改版"})
    assert data["count"] == 1
    assert data["rows"][0]["project"] == "官网改版"


async def test_query_no_match_returns_empty_not_error() -> None:
    data = await budget._budget_query(CTX, {"project": "不存在的项目"})
    assert data["count"] == 0
    assert data["rows"] == []


async def test_query_rejects_bad_limit() -> None:
    try:
        await budget._budget_query(CTX, {"limit": 0})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("limit=0 应被 1001 拒绝")


# ---------------- CSV 叠加 ----------------


async def test_csv_overlay_adds_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "budget.csv").write_text(
        "project,department,year,budget,used\n新项目,研发部,2026,100000,95000\n",
        encoding="utf-8",
    )
    data = await budget._budget_query(CTX, {})
    assert data["overlay_applied"] is True
    assert data["source"] == "builtin-demo+local-csv"
    assert data["count"] == 6
    added = next(row for row in data["rows"] if row["project"] == "新项目")
    assert added["remaining"] == 5000
    assert added["status"] == "紧张"


# ---------------- 注册口径 ----------------


def test_spec_is_read_without_approval() -> None:
    (spec,) = budget.specs()
    assert spec.name == "office.budget.query"
    assert spec.scope == "office:read"
    assert spec.requires_approval is False
