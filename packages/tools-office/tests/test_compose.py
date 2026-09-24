"""通用文案起草 + 月报扩展单测（handler 直调，不起 HTTP 服务）。

覆盖：compose 五类模板渲染/缺板块留白/空要点与未知类型拒绝；report.generate 月报
      板块与缺省留白（周报/日报回归不断）。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.1（自动生成）。
"""

from __future__ import annotations

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_tools_office import compose, tools

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read"])


# ---------------- 通用文案 ----------------


async def test_compose_notice_renders_header_points_footer() -> None:
    """通知公告：抬头/要点/落款齐备渲染。"""
    data = await compose._memo_compose(
        CTX,
        {
            "kind": "notice",
            "title": "放假安排",
            "points": ["10 月 1 日至 7 日放假", "10 月 8 日正常上班"],
            "header": {"to": "全体员工", "from": "行政部", "date": "2026-09-30"},
            "footer": "行政部",
        },
    )
    assert data["kind_label"] == "通知公告"
    assert "致：全体员工" in data["document"]
    assert "1. 10 月 1 日至 7 日放假" in data["document"]
    assert data["point_count"] == 2


async def test_compose_blank_sections_without_fabrication() -> None:
    """缺抬头落款留白（邮件无称呼无落款不断章取义）。"""
    data = await compose._memo_compose(
        CTX, {"kind": "email", "title": "进度同步", "points": ["联调完成"]}
    )
    assert "（未提供落款，留白不编造）" in data["document"]


async def test_compose_rejects_empty_points() -> None:
    """空要点拒绝（无正文不编造全文）。"""
    try:
        await compose._memo_compose(CTX, {"kind": "proposal", "title": "方案", "points": []})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("空要点应被 1001 拒绝")


async def test_compose_rejects_unknown_kind() -> None:
    """未知类型拒绝。"""
    try:
        await compose._memo_compose(CTX, {"kind": "poem", "title": "诗", "points": ["一句"]})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("未知类型应被 1001 拒绝")


def test_compose_registers_read_tool() -> None:
    """注册口径：读 scope、免审批。"""
    specs = {spec.name: spec for spec in compose.specs()}
    assert set(specs) == {"office.memo.compose"}
    assert specs["office.memo.compose"].scope == "office:read"
    assert specs["office.memo.compose"].requires_approval is False


# ---------------- 月报扩展 ----------------


async def test_report_monthly_adds_sections() -> None:
    """月报追加本月重点/下月计划板块（复用 highlights/next_plan 入参）。"""
    data = await tools._report_generate(
        CTX,
        {
            "title": "9 月月报",
            "report_type": "monthly",
            "metrics": {"交付需求": 30},
            "highlights": ["上线审批闭环"],
            "next_plan": ["接入考勤台账"],
        },
    )
    assert data["report_type"] == "monthly"
    assert "报告类型：月报" in data["report"]
    assert "## 三、本月重点" in data["report"]
    assert "- 上线审批闭环" in data["report"]
    assert "## 四、下月计划" in data["report"]


async def test_report_monthly_missing_sections_blank() -> None:
    """月报缺板块留白（与周报同口径）。"""
    data = await tools._report_generate(
        CTX, {"title": "9 月月报", "report_type": "monthly", "metrics": {"交付需求": 30}}
    )
    assert "本月重点：未提供，留白不编造" in data["report"]
    assert "下月计划：未提供，留白不编造" in data["report"]
