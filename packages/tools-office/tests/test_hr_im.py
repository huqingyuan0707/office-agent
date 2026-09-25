"""PRD §2.7 群消息摘要 + §2.8 人事行政单元测试（handler 直调，不起 HTTP 服务）。

覆盖：im.digest（@本人摘录/任务候选/问句决议风险三类/口径拒绝）、
      hr.attendance（出勤计数/工时加班确定性计算/缺人拒绝）、hr.checklist
      （入离职模板/交接缺项留白/非法场景拒绝）、resource.query/book
      （台账占用/冲突拒绝/非法时间拒绝）。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.7/§2.8。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_core.settings import settings
from office_agent_tools_office import hr, im_digest, resources

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read", "office:write"])


@pytest.fixture
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """测试隔离的文档工作目录（预订台账不污染真实 DOCS_DIR）。"""
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    return tmp_path


_MSGS = [
    {"sender": "张三", "text": "@李四 请明天下班前提交接口文档。"},
    {"sender": "李四", "text": "收到。大家注意，发布有延期风险，测试环境阻塞。"},
    {"sender": "王五", "text": "上线方案通过了，就这么定。周报什么时候交？"},
]


# ---------------- 群消息摘要 ----------------


async def test_digest_mentions_tasks_and_key_points() -> None:
    data = await im_digest._im_digest(CTX, {"messages": _MSGS, "me": "李四", "source": "feishu"})
    assert data["total"] == 3
    assert data["by_sender"] == {"张三": 1, "李四": 1, "王五": 1}
    assert len(data["mentions"]) == 1 and data["mentions"][0]["sender"] == "张三"
    assert len(data["my_tasks"]) == 1
    assert data["my_tasks"][0]["due"] == "明天"
    assert data["my_tasks"][0]["from"] == "张三"
    assert any("什么时候交" in q for q in data["questions"])
    assert any("就这么定" in d for d in data["decisions"])
    assert any("阻塞" in r for r in data["risks"])
    assert "@李四" in data["brief"] or "李四" in data["brief"]


async def test_digest_quiet_stream_says_so() -> None:
    data = await im_digest._im_digest(
        CTX, {"messages": [{"sender": "张三", "text": "今日同步完毕。"}], "me": "李四"}
    )
    assert data["my_tasks"] == []
    assert "一般同步" in data["brief"]


async def test_digest_rejects_bad_input() -> None:
    bad = [
        {"messages": [], "me": "李四"},  # 空流
        {"messages": _MSGS, "me": ""},  # 缺本人
        {"messages": _MSGS, "me": "李四", "source": "dingtalk"},  # 非法来源
        {"messages": [{"sender": "", "text": "hi"}], "me": "李四"},  # 空发送人
    ]
    for args in bad:
        try:
            await im_digest._im_digest(CTX, args)
        except BusinessError as exc:
            assert exc.code == 1001
        else:
            raise AssertionError(f"非法入参 {args} 应被 1001 拒绝")


# ---------------- 考勤加班 ----------------


async def test_attendance_sums_status_and_overtime() -> None:
    data = await hr._hr_attendance(CTX, {"person": "李四"})
    assert data["status_count"].get("迟到") == 1
    assert data["total_hours"] == 17.0  # 8 + 9
    assert data["overtime_hours"] == 1.0  # max(0,9-8)
    assert "max(0" in data["overtime_rule"]
    assert data["source"] == "builtin-demo"
    assert data["fetched_at"]


async def test_attendance_rejects_missing_person() -> None:
    try:
        await hr._hr_attendance(CTX, {"person": ""})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("缺 person 应被 1001 拒绝")


# ---------------- 入离职清单 ----------------


async def test_checklist_onboard_sections() -> None:
    data = await hr._hr_checklist(CTX, {"scene": "onboard", "person": "新人"})
    assert len(data["sections"]) == 5
    assert "新人" in data["sections"][0]
    assert data["unfilled"] == []


async def test_checklist_offboard_handover_blanks() -> None:
    data = await hr._hr_checklist(
        CTX,
        {
            "scene": "offboard",
            "person": "老王",
            "handover": [
                {"item": "客服知识库权限"},
                {"item": "报销单", "owner": "小李", "due": "月底"},
            ],
        },
    )
    assert len(data["reminders"]) == 2
    assert "【待补充】" in data["reminders"][0]
    assert "小李" in data["reminders"][1]
    assert data["unfilled"] != []


async def test_checklist_rejects_bad_scene() -> None:
    try:
        await hr._hr_checklist(CTX, {"scene": "transfer"})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("非法场景应被 1001 拒绝")


# ---------------- 资源查询与预订 ----------------


async def test_resource_query_lists_with_slots(docs_dir: Path) -> None:
    _ = docs_dir
    data = await resources._resource_query(CTX, {"type": "room", "date": "2026-09-30"})
    assert data["count"] == 2
    assert all(res["type"] == "room" for res in data["resources"])
    assert all(res["booked_slots"] == [] for res in data["resources"])


async def test_resource_book_conflict_rejects(docs_dir: Path) -> None:
    _ = docs_dir
    first = await resources._resource_book(
        CTX,
        {
            "resource_id": "room-big",
            "date": "2026-09-30",
            "start": "10:00",
            "end": "11:00",
            "purpose": "冒烟评审",
            "idem_key": "hr-im-0001",
        },
    )
    assert first["resource_name"] == "大会议室"
    try:
        await resources._resource_book(
            CTX,
            {
                "resource_id": "room-big",
                "date": "2026-09-30",
                "start": "10:30",
                "end": "11:30",
                "purpose": "撞车会议",
                "idem_key": "hr-im-0002",
            },
        )
    except BusinessError as exc:
        assert exc.code == 1001 and "已被预订" in str(exc)
    else:
        raise AssertionError("区间重叠预订应被 1001 拒绝")
    back_to_back = await resources._resource_book(
        CTX,
        {
            "resource_id": "room-big",
            "date": "2026-09-30",
            "start": "11:00",
            "end": "12:00",
            "purpose": "紧接会议",
            "idem_key": "hr-im-0003",
        },
    )
    assert back_to_back["start"] == "11:00"  # 首尾相接不算重叠


async def test_resource_book_rejects_bad_slot_and_unknown(docs_dir: Path) -> None:
    _ = docs_dir
    bad = [
        {"resource_id": "room-nope", "date": "2026-09-30", "start": "10:00", "end": "11:00"},
        {"resource_id": "room-big", "date": "明天", "start": "10:00", "end": "11:00"},
        {"resource_id": "room-big", "date": "2026-09-30", "start": "11:00", "end": "10:00"},
    ]
    for args in bad:
        try:
            await resources._resource_book(CTX, {**args, "idem_key": "hr-im-0009"})
        except BusinessError as exc:
            assert exc.code == 1001
        else:
            raise AssertionError(f"非法预订 {args} 应被 1001 拒绝")


# ---------------- 注册口径 ----------------


def test_specs_scopes_and_approval() -> None:
    digest = {spec.name: spec for spec in im_digest.specs()}
    assert set(digest) == {"office.im.digest"}
    assert digest["office.im.digest"].scope == "office:read"
    hr_specs = {spec.name: spec for spec in hr.specs()}
    assert set(hr_specs) == {"office.hr.attendance", "office.hr.checklist"}
    res = {spec.name: spec for spec in resources.specs()}
    assert set(res) == {"office.resource.query", "office.resource.book"}
    assert res["office.resource.book"].requires_approval is True
    assert res["office.resource.query"].requires_approval is False
