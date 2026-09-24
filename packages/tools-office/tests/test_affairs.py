"""个人事务工具单测（affairs.py 待办域 + affairs_schedule.py 日程域，handler 直调）。

覆盖：待办创建落盘与清单、状态过滤、标记完成记 completed_at、越权/不存在 404、
      删除、日程创建（会议时长/节点零时长/坏时间 1001）、空闲区间实测计算、
      台账窗口聚合、租户隔离。
对齐：AGENTS.md §5；智能办公Agent 产品需求文档.md §2.2。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import settings
from office_agent_tools_office import affairs, affairs_schedule, tools

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read", "office:write"])
CTX_BOB = ToolContext(tenant="t1", username="bob", roles=["office:read", "office:write"])


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """事务存储锁进临时目录（每例独立文件）。"""
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    return tmp_path


# ---------------- 待办 ----------------


async def test_todo_create_persists_then_lists(store: Path) -> None:
    receipt = await tools._todo_create(
        CTX, {"title": "交周报", "priority": "high", "due_date": "2026-09-26"}
    )
    assert receipt["status"] == "open" and receipt["owner"] == "alice"
    data = await affairs._todo_list(CTX, {})
    assert data["count"] == 1
    assert data["items"][0]["title"] == "交周报"
    assert data["source"].startswith("local-affairs-store")


async def test_todo_list_status_filter_and_done(store: Path) -> None:
    rec = await affairs.add_todo("t1", "alice", title="A", due_date="2026-09-26")
    await affairs.add_todo("t1", "alice", title="B")
    updated = await affairs.update_todo("t1", "alice", rec["id"], {"status": "done"})
    assert updated["completed_at"], "标记完成必须记 completed_at"
    open_data = await affairs._todo_list(CTX, {"status": "open"})
    all_data = await affairs._todo_list(CTX, {"status": "all"})
    assert [t["title"] for t in open_data["items"]] == ["B"]
    assert all_data["count"] == 2


async def test_todo_update_rejects_bad_status_and_field(store: Path) -> None:
    rec = await affairs.add_todo("t1", "alice", title="A")
    with pytest.raises(BusinessError, match="open / done / cancelled"):
        await affairs.update_todo("t1", "alice", rec["id"], {"status": "archived"})
    with pytest.raises(BusinessError, match="不支持修改的字段"):
        await affairs.update_todo("t1", "alice", rec["id"], {"owner": "bob"})


async def test_todo_update_missing_or_foreign_404(store: Path) -> None:
    rec = await affairs.add_todo("t1", "alice", title="A")
    with pytest.raises(BusinessError) as exc:
        await affairs.update_todo("t1", "bob", rec["id"], {"status": "done"})
    assert exc.value.code == ErrorCode.NOT_FOUND
    data = await affairs._todo_list(CTX_BOB, {})
    assert data["count"] == 0, "他人待办不可见"


async def test_todo_delete_removes_and_second_404(store: Path) -> None:
    rec = await affairs.add_todo("t1", "alice", title="A")
    receipt = await affairs.delete_todo("t1", "alice", rec["id"])
    assert receipt["deleted"] == rec["id"] and receipt["title"] == "A"
    with pytest.raises(BusinessError):
        await affairs.delete_todo("t1", "alice", rec["id"])


# ---------------- 日程 ----------------


async def test_schedule_create_meeting_computes_end(store: Path) -> None:
    data = await affairs_schedule._schedule_create(
        CTX,
        {
            "title": "需求评审",
            "kind": "meeting",
            "start": "2026-09-26 10:00",
            "duration_minutes": 90,
            "attendees": ["bob", " carol ", ""],
            "location": "3 号会议室",
        },
    )
    created = data["created"]
    assert created["end"] == "2026-09-26 11:30"
    assert data["invited"] == ["bob", "carol"], "去空白与去空值"


async def test_schedule_create_milestone_and_bad_input(store: Path) -> None:
    data = await affairs_schedule._schedule_create(
        CTX, {"title": "上线节点", "kind": "milestone", "start": "2026-09-28 09:00"}
    )
    assert data["created"]["end"] == "", "节点零时长不落 end"
    with pytest.raises(BusinessError, match="YYYY-MM-DD HH:MM"):
        await affairs_schedule._schedule_create(CTX, {"title": "坏", "start": "后天早上"})
    with pytest.raises(BusinessError, match="kind"):
        await affairs_schedule._schedule_create(
            CTX, {"title": "坏", "kind": "party", "start": "2026-09-26 10:00"}
        )


async def test_freebusy_computes_gaps_for_owner_and_attendee(store: Path) -> None:
    await affairs_schedule.add_schedule(
        "t1",
        "alice",
        title="评审",
        kind="meeting",
        start="2026-09-26 10:00",
        duration_minutes=90,
        attendees=["bob"],
    )
    data = await affairs_schedule._schedule_freebusy(CTX, {"date": "2026-09-26"})
    alice = data["persons"][0]
    assert alice["busy"] == [{"from": "10:00", "to": "11:30", "title": "评审"}]
    assert alice["free"] == [
        {"from": "09:00", "to": "10:00"},
        {"from": "11:30", "to": "18:00"},
    ]
    both = await affairs_schedule._schedule_freebusy(
        CTX, {"date": "2026-09-26", "persons": ["bob", "dave"]}
    )
    by_name = {p["person"]: p for p in both["persons"]}
    assert by_name["bob"]["busy"], "参会人应看到同一场会"
    assert not by_name["dave"]["busy"] and len(by_name["dave"]["free"]) == 1


# ---------------- 台账 ----------------


async def test_worklog_aggregates_window(store: Path) -> None:
    done = await affairs.add_todo("t1", "alice", title="已完成项", due_date="2026-09-24")
    await affairs.update_todo("t1", "alice", done["id"], {"status": "done"})
    await affairs.add_todo("t1", "alice", title="待办项", due_date="2026-09-26")
    await affairs_schedule.add_schedule(
        "t1",
        "alice",
        title="周会",
        kind="meeting",
        start="2026-09-25 09:00",
        duration_minutes=60,
        attendees=[],
    )
    data = await affairs_schedule._worklog_generate(
        CTX, {"period": "weekly", "end_date": "2026-09-26"}
    )
    assert data["counts"] == {"done": 1, "pending": 1, "schedules": 1}
    assert (
        "已完成项" in data["worklog"] and "待办项" in data["worklog"] and "周会" in data["worklog"]
    )
    assert data["source"].startswith("local-affairs-store")


async def test_worklog_rejects_bad_period(store: Path) -> None:
    with pytest.raises(BusinessError, match="daily / weekly"):
        await affairs_schedule._worklog_generate(CTX, {"period": "monthly"})


# ---------------- 隔离与共用入口 ----------------


async def test_load_affairs_tenant_isolation(store: Path) -> None:
    await affairs.add_todo("t1", "alice", title="T1")
    await affairs.add_todo("t2", "alice", title="T2")
    data = await affairs.load_affairs("t1")
    assert [t["title"] for t in data["todos"]] == ["T1"]
