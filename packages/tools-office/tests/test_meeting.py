"""会议全流程协作工具单元测试（handler 直调，不起 HTTP 服务）。

覆盖：议程模板直出与缺板块留白、预约强时间格式与非法拒绝、风险关键词命中分级与
      无命中 degraded 不编造、注册聚合口径。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.5（V1.1 会议协作）。
"""

from __future__ import annotations

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_tools_office import meeting

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read", "office:write"])


# ---------------- 会前议程 ----------------


async def test_agenda_renders_topics_and_attendees() -> None:
    data = await meeting._meeting_agenda(
        CTX,
        {
            "title": "周会",
            "objective": "对齐进度",
            "topics": ["进展同步", "风险评审"],
            "attendees": ["张三", "李四"],
            "duration_minutes": 45,
        },
    )
    assert "会议议程：周会" in data["agenda"]
    assert "1. 进展同步" in data["agenda"]
    assert "预计时长：45 分钟" in data["agenda"]
    assert data["counts"] == {"topic": 2, "attendee": 2}


async def test_agenda_blank_when_missing() -> None:
    data = await meeting._meeting_agenda(CTX, {"title": "临时会"})
    assert "未提供会议目标" in data["agenda"]
    assert "未提供议题" in data["agenda"]


async def test_agenda_rejects_bad_duration() -> None:
    try:
        await meeting._meeting_agenda(CTX, {"title": "周会", "duration_minutes": 0})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("非法时长应被 1001 拒绝")


# ---------------- 会议预约 ----------------


async def test_book_returns_receipt_with_owner() -> None:
    data = await meeting._meeting_book(
        CTX,
        {
            "title": "项目评审",
            "start_time": "2026-10-10 14:00",
            "duration_minutes": 60,
            "attendees": ["张三"],
        },
    )
    assert data["start_time"] == "2026-10-10 14:00"
    assert data["owner"] == "alice"
    assert data["attendees"] == ["张三"]


async def test_book_rejects_fuzzy_time() -> None:
    try:
        await meeting._meeting_book(CTX, {"title": "项目评审", "start_time": "明天下午"})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("含糊时间应被 1001 拒绝")


# ---------------- 风险预警 ----------------


async def test_risks_hits_keyword_with_level_and_excerpt() -> None:
    data = await meeting._meeting_risks(
        CTX, {"minutes": "联调出现阻塞，前端依赖的接口延期两天，整体风险可控。"}
    )
    assert data["count"] == 4
    assert data["high_count"] == 1
    levels = {hit["keyword"]: hit["level"] for hit in data["risks"]}
    assert levels == {"阻塞": "高", "依赖": "中", "延期": "中", "风险": "低"}
    assert all(hit["excerpt"] for hit in data["risks"])
    assert data["degraded"] is False


async def test_risks_no_hit_degrades_without_fabrication() -> None:
    data = await meeting._meeting_risks(CTX, {"minutes": "会议顺利结束，各项决议一致通过。"})
    assert data["risks"] == []
    assert data["degraded"] is True
    assert "不编造风险" in data["degraded_reason"]


async def test_risks_rejects_empty_minutes() -> None:
    try:
        await meeting._meeting_risks(CTX, {"minutes": "  "})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("空纪要应被 1001 拒绝")


# ---------------- 注册聚合 ----------------


def test_specs_register_two_read_one_write() -> None:
    specs = {spec.name: spec for spec in meeting.specs()}
    assert set(specs) == {"office.meeting.agenda", "office.meeting.book", "office.meeting.risks"}
    assert specs["office.meeting.book"].requires_approval is True
    assert specs["office.meeting.book"].scope == "office:write"
    assert specs["office.meeting.agenda"].requires_approval is False
    assert specs["office.meeting.risks"].requires_approval is False
