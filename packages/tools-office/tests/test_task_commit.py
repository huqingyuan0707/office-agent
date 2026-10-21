"""任务拆解批量建单单测（office.task.commit 真实落盘 + 幂等回放 + 不臆造口径）。

覆盖：批量建单真实落待办存储（归属各责任人）、同 idem_key 重放回放原回执绝不双单、
      缺责任人/缺标题/坏日期/短幂等键执行期诚实 1001、待办在各责任人清单里可见。
对齐：AGENTS.md §5；智能办公Agent 产品需求文档.md §2.9、§6.5。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import settings
from office_agent_tools_office import affairs, task_planner

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read", "office:write"])
CTX_BOB = ToolContext(tenant="t1", username="bob", roles=["office:read", "office:write"])

_TASKS = [
    {"title": "需求调研与方案定稿", "owner": "产品", "due_date": "2026-10-02", "priority": "high"},
    {"title": "前端页面开发", "owner": "前端", "due_date": "2026-10-16", "priority": "medium"},
]


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """事务存储锁进临时目录（每例独立文件）。"""
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    return tmp_path


async def test_commit_lands_todos_for_each_owner(store: Path) -> None:
    """批量建单真实落盘：每条待办归属对应责任人，可在各自清单里看到。"""
    receipt = await task_planner._task_commit(CTX, {"tasks": _TASKS, "idem_key": "commit-0001"})
    assert receipt["count"] == 2
    assert receipt["replayed"] is False
    assert receipt["notified_owners"] == ["产品", "前端"]
    assert all(row["id"] for row in receipt["created"])

    data = await affairs.load_affairs("t1")
    assert len(data["todos"]) == 2
    assert {todo["owner"] for todo in data["todos"]} == {"产品", "前端"}
    assert {todo["status"] for todo in data["todos"]} == {"open"}


async def test_commit_same_idem_key_replays_without_duplicate(store: Path) -> None:
    """同 idem_key 重放：回放原回执，绝不双单。"""
    first = await task_planner._task_commit(CTX, {"tasks": _TASKS, "idem_key": "commit-0002"})
    second = await task_planner._task_commit(CTX, {"tasks": _TASKS, "idem_key": "commit-0002"})
    assert second["replayed"] is True
    assert [row["id"] for row in second["created"]] == [row["id"] for row in first["created"]]
    data = await affairs.load_affairs("t1")
    assert len(data["todos"]) == 2, "重放不得重复建单"


async def test_commit_missing_owner_is_rejected(store: Path) -> None:
    """缺责任人执行期诚实 1001（不臆造接收人，也不落半截台账）。"""
    with pytest.raises(BusinessError) as exc:
        await task_planner._task_commit(
            CTX, {"tasks": [{"title": "上线准备"}], "idem_key": "commit-0003"}
        )
    assert exc.value.code == ErrorCode.PARAM_INVALID
    assert "责任人" in exc.value.msg
    data = await affairs.load_affairs("t1")
    assert data["todos"] == []


async def test_commit_rejects_bad_title_date_priority_and_key(store: Path) -> None:
    """缺标题 / 坏日期 / 非法优先级 / 短幂等键一律 1001。"""
    with pytest.raises(BusinessError):
        await task_planner._task_commit(
            CTX, {"tasks": [{"owner": "产品"}], "idem_key": "commit-0004"}
        )
    with pytest.raises(BusinessError):
        await task_planner._task_commit(
            CTX,
            {
                "tasks": [{"title": "A", "owner": "产品", "due_date": "10月2日"}],
                "idem_key": "commit-0005",
            },
        )
    with pytest.raises(BusinessError):
        await task_planner._task_commit(
            CTX,
            {
                "tasks": [{"title": "A", "owner": "产品", "priority": "urgent"}],
                "idem_key": "commit-0006",
            },
        )
    with pytest.raises(BusinessError):
        await task_planner._task_commit(CTX, {"tasks": _TASKS, "idem_key": "short"})
    data = await affairs.load_affairs("t1")
    assert data["todos"] == [], "校验不过不落任何待办"


async def test_commit_todos_visible_in_owner_list(store: Path) -> None:
    """建单后的待办在责任人自己的清单里可见（跨人不可见）。"""
    await task_planner._task_commit(CTX, {"tasks": _TASKS, "idem_key": "commit-0007"})
    owner_ctx = ToolContext(tenant="t1", username="前端", roles=["office:read"])
    data = await affairs._todo_list(owner_ctx, {})
    assert data["count"] == 1
    assert data["items"][0]["title"] == "前端页面开发"
    other = await affairs._todo_list(CTX_BOB, {})
    assert other["count"] == 0
