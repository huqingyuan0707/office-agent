"""审批智能辅助工具单元测试（handler 直调，不起 HTTP 服务）。

覆盖：五类单草稿生成与缺项追问、合规自查（金额分级/高危二次确认/日期倒挂）、
      发票要素提取与无命中 degraded、注册聚合口径。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.3（V1.1 审批辅助）。
"""

from __future__ import annotations

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_tools_office import approval

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read"])


# ---------------- 草稿生成 ----------------


async def test_draft_ready_when_fields_complete() -> None:
    data = await approval._approval_draft(
        CTX,
        {
            "kind": "leave",
            "fields": {
                "leave_type": "年假",
                "start_date": "2026-10-10",
                "end_date": "2026-10-11",
                "reason": "休假",
            },
        },
    )
    assert data["ready"] is True
    assert data["missing_fields"] == []
    assert data["applicant"] == "alice"
    assert "请假申请草稿" in data["summary"]


async def test_draft_missing_fields_ask_back() -> None:
    data = await approval._approval_draft(CTX, {"kind": "expense", "fields": {"reason": "打车"}})
    assert data["ready"] is False
    assert "报销金额" in data["missing_fields"]
    assert "费用发生日期" in data["missing_fields"]
    assert "请补充" in data["next_hint"]


async def test_draft_rejects_unknown_kind() -> None:
    try:
        await approval._approval_draft(CTX, {"kind": "resign"})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("未知单据类型应被 1001 拒绝")


# ---------------- 合规自查 ----------------


async def test_check_small_amount_passes() -> None:
    data = await approval._approval_check(
        CTX,
        {
            "kind": "expense",
            "fields": {"amount": 500, "expense_date": "2026-09-20", "reason": "打车"},
        },
    )
    assert data["passed"] is True
    assert data["need_confirm"] is False


async def test_check_mid_amount_hints_dept_head() -> None:
    data = await approval._approval_check(
        CTX,
        {
            "kind": "expense",
            "fields": {"amount": 2000, "expense_date": "2026-09-20", "reason": "招待"},
        },
    )
    assert data["passed"] is True
    assert data["need_confirm"] is False
    assert any("部门负责人" in hint for hint in data["hints"])


async def test_check_high_amount_requires_confirm() -> None:
    data = await approval._approval_check(
        CTX,
        {
            "kind": "expense",
            "fields": {"amount": 8000, "expense_date": "2026-09-20", "reason": "设备"},
        },
    )
    assert data["need_confirm"] is True
    assert "二次确认" in data["confirm_tip"]
    assert any("分管副总" in hint for hint in data["hints"])


async def test_check_rejects_inverted_leave_dates() -> None:
    data = await approval._approval_check(
        CTX,
        {
            "kind": "leave",
            "fields": {
                "leave_type": "年假",
                "start_date": "2026-10-12",
                "end_date": "2026-10-10",
                "reason": "休假",
            },
        },
    )
    assert data["passed"] is False
    assert any("晚于" in issue for issue in data["issues"])


# ---------------- 发票提取 ----------------


async def test_invoice_extracts_four_elements() -> None:
    data = await approval._invoice_extract(
        CTX,
        {
            "text": "增值税普通发票\n发票号码：12345678\n开票日期：2026年9月20日\n"
            "销售方：北京市某某办公用品有限公司\n合计：¥1,280.00"
        },
    )
    assert data["extracted"]["amount"] == 1280.0
    assert data["extracted"]["invoice_no"] == "12345678"
    assert data["extracted"]["bill_date"] == "2026年9月20日"
    assert "办公用品" in data["extracted"]["seller"]
    assert data["degraded"] is False


async def test_invoice_no_hit_degrades_without_fabrication() -> None:
    data = await approval._invoice_extract(CTX, {"text": "会议纪要：周会讨论项目进展。"})
    assert data["extracted"] == {"amount": "", "invoice_no": "", "bill_date": "", "seller": ""}
    assert data["degraded"] is True


async def test_invoice_rejects_empty_text() -> None:
    try:
        await approval._invoice_extract(CTX, {"text": "  "})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("空文本应被 1001 拒绝")


# ---------------- 注册聚合 ----------------


def test_specs_register_three_read_tools() -> None:
    specs = {spec.name: spec for spec in approval.specs()}
    assert set(specs) == {
        "office.approval.draft",
        "office.approval.check",
        "office.invoice.extract",
    }
    assert all(spec.scope == "office:read" for spec in specs.values())
    assert all(spec.requires_approval is False for spec in specs.values())
