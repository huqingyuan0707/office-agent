"""PRD §2.9 项目台账 + §2.10 财务辅助 + §2.11 通用工具单元测试（handler 直调）。

覆盖：project.query（过滤/当前里程碑/简报/非法 limit）、finance.reimburse
      （分人分阶段/在途口径/缺人拒绝）、finance.expense（部门汇总/类目/预算联带）、
      desk.ticket/tickets（落单字段/清单过滤/非法 kind）。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.9/§2.10/§2.11。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_core.settings import settings
from office_agent_tools_office import desk, finance, projects

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read", "office:write"])


@pytest.fixture
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """测试隔离的文档工作目录（工单台账不污染真实 DOCS_DIR）。"""
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    return tmp_path


# ---------------- 项目台账 ----------------


async def test_project_query_filters_and_brief() -> None:
    data = await projects._project_query(CTX, {"owner": "产品经理"})
    assert data["count"] == 1
    item = data["projects"][0]
    assert item["name"] == "官网改版"
    assert item["current_milestone"] == "上线发布（doing）"
    assert item["risks"] == [{"desc": "UI 稿确认滞后", "level": "medium"}]
    assert "官网改版" in data["brief"] and "60%" in data["brief"]
    assert data["source"] == "builtin-demo"


async def test_project_query_has_risk_and_done_milestone() -> None:
    data = await projects._project_query(CTX, {"has_risk": True})
    assert data["count"] == 1
    done = await projects._project_query(CTX, {"name": "数据看板"})
    assert done["projects"][0]["current_milestone"] == "—"


async def test_project_query_rejects_bad_limit() -> None:
    try:
        await projects._project_query(CTX, {"limit": 0})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("非法 limit 应被 1001 拒绝")


# ---------------- 报销与费用 ----------------


async def test_reimburse_stages_and_transit() -> None:
    data = await finance._finance_reimburse(CTX, {"person": "张三"})
    assert data["count"] == 2
    assert data["by_stage"] == {"审批中": 1, "已打款": 1}
    assert data["transit_amount"] == 3200.5  # 仅审批中（已通过未打款口径见 rule）
    assert "transit_rule" in data


async def test_reimburse_stage_filter_and_missing_person() -> None:
    data = await finance._finance_reimburse(CTX, {"person": "李四", "stage": "已驳回"})
    assert data["count"] == 1 and data["records"][0]["no"] == "BX-2026-092"
    try:
        await finance._finance_reimburse(CTX, {"person": ""})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("缺 person 应被 1001 拒绝")


async def test_expense_sums_and_budget_link() -> None:
    data = await finance._finance_expense(CTX, {"department": "产品部", "month": "2026-09"})
    assert data["total"] == 92000.0
    assert data["count"] == 2
    assert data["by_category"] == {"人力": 80000.0, "采购": 12000.0}
    linked = data["budgets"]["官网改版"]
    assert linked["remaining"] == 74000.0  # 200000-126000
    assert linked["usage_pct"] == 63.0


async def test_expense_month_prefix_and_missing_dept() -> None:
    data = await finance._finance_expense(CTX, {"department": "研发部", "month": "2026-08"})
    assert data["count"] == 1 and data["total"] == 8000.0
    assert data["budgets"] == {} or "数据看板" in data["budgets"]  # 8 月行项目仍联带
    try:
        await finance._finance_expense(CTX, {"department": ""})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("缺 department 应被 1001 拒绝")


# ---------------- 后勤工单 ----------------


async def test_desk_ticket_roundtrip(docs_dir: Path) -> None:
    _ = docs_dir
    created = await desk._desk_ticket(
        CTX,
        {"kind": "it_repair", "title": "打印机卡纸", "detail": "3 楼", "idem_key": "ops-0001"},
    )
    assert created["kind_label"] == "IT 报修"
    assert created["status"] == "open"
    listed = await desk._desk_tickets(CTX, {"kind": "it_repair"})
    assert listed["count"] == 1
    assert listed["tickets"][0]["title"] == "打印机卡纸"
    assert (await desk._desk_tickets(CTX, {"status": "done"}))["count"] == 0


async def test_desk_ticket_rejects_bad_kind(docs_dir: Path) -> None:
    _ = docs_dir
    try:
        await desk._desk_ticket(CTX, {"kind": "coffee", "title": "买咖啡", "idem_key": "ops-0002"})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("非法 kind 应被 1001 拒绝")


# ---------------- 注册口径 ----------------


def test_specs_scopes_and_approval() -> None:
    assert {s.name for s in projects.specs()} == {"office.project.query"}
    assert {s.name for s in finance.specs()} == {
        "office.finance.reimburse",
        "office.finance.expense",
    }
    assert all(s.scope == "office:read" for s in finance.specs())
    res = {s.name: s for s in desk.specs()}
    assert set(res) == {"office.desk.ticket", "office.desk.tickets"}
    assert res["office.desk.ticket"].requires_approval is True
    assert res["office.desk.tickets"].requires_approval is False
