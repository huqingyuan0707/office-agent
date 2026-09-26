"""LangGraph 图结构层单测（ADR-0005 阶段一）：route 裁决 / 边界收敛形状锁定 / 并发不串

口径：全程不出网不建库——边界续跑路径不触 executor，用最小假会话（commit/add 空实现）
与预置 checkpoint 驱动图；HTTP 级全链路（送审挂起→批准→续跑）由 test_runtime.py 覆盖。
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.graph import END

from office_agent_core.contracts import AgentState
from office_agent_runtime import graph, loop_state
from office_agent_runtime.checkpoint import dumps
from office_agent_runtime.loop_state import LoopState
from office_agent_runtime.spec import parse_agent_spec
from office_agent_server.models import Task
from office_agent_server.rbac import CurrentUser


class _FakeDb:
    """最小会话替身：本文件路径只用到 add（幻影步骤断言计数）与 commit（锁纪律）。"""

    def __init__(self) -> None:
        self.commits = 0
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


def _spec(name: str = "g-test") -> object:
    return parse_agent_spec(
        {
            "name": name,
            "tools": ["demo.echo"],
            "max_steps": 3,
            "rules": [
                {"match": ["演示"], "steps": [{"tool": "demo.echo", "args": {"text": "hi"}}]}
            ],
        }
    )


def _user() -> CurrentUser:
    return CurrentUser(username="admin", tenant="default", roles=["*"])


def _task(checkpoint: dict) -> Task:
    return Task(
        tenant="default",
        username="admin",
        type="agent.run",
        status=AgentState.IDLE.value,
        input=dumps({}),
        checkpoint=dumps(checkpoint),
    )


def _boundary_checkpoint(goal: str = "演示") -> dict:
    """续跑边界检查点：单步计划且步 0 已 ok、断点在末尾（next_step=1）。"""
    return {
        "agent": "g-test",
        "goal": goal,
        "plan": [{"tool": "demo.echo", "args": {"text": "hi"}}],
        "planner_source": "rule",
        "next_step": 1,
        "steps": [
            {
                "index": 0,
                "tool": "demo.echo",
                "status": "ok",
                "outcome": {"result": {"text": "hi"}},
            }
        ],
        "error": "",
    }


@pytest.fixture(autouse=True)
def _no_llm_finalize(monkeypatch):
    """finish() 的终答合成打桩（本文件不出网；finalize_answer 归 validation 测试管）。"""

    async def _empty(**_kw: object) -> dict:
        return {}

    monkeypatch.setattr(loop_state, "finalize_answer", _empty)


@pytest.mark.parametrize(
    ("route", "expected"),
    [("stop", END), ("again", "act"), ("act", "act")],
)
def test_route_arbitration_table_driven(route: str, expected: str) -> None:
    """条件边裁决纯函数：stop 停图，其余继续自环（act 节点即原 while 循环）。"""
    state = {"ls": None, "planned": [], "route": route}
    assert graph._route_stop_or(state, "act") == expected  # type: ignore[arg-type]


async def test_boundary_concludes_done_without_phantom_step() -> None:
    """续跑边界（断点已在末尾）：图收敛 DONE，不新增步骤条目/幻影步骤行，形状与旧实现一致。"""
    db = _FakeDb()
    ls = LoopState(db, _task(_boundary_checkpoint()), _spec(), "演示", _user(), "trace-1")
    await graph.run_graph(ls)

    assert ls.task.status == AgentState.DONE.value
    assert ls.index == 1
    assert len(ls.checkpoint["steps"]) == 1  # 边界收敛不加条目
    assert db.added == []  # 无幻影 RunStep 行
    assert {"agent", "goal", "plan", "planner_source", "next_step", "steps", "error"} <= set(
        ls.checkpoint
    )  # 形状锁定：checkpoint 键集合不漂
    summary = await ls.finish()
    assert summary["status"] == AgentState.DONE.value
    assert db.commits >= 1  # 锁纪律：finish 进 finalize_answer 前先 commit


async def test_plan_failure_converges_failed_in_graph() -> None:
    """规划失败（规则未命中且无 LLM）在 plan 节点收口：FAILED 终态 + 可操作原因进检查点。"""
    task = _task({"agent": "g-test", "goal": "无关目标", "next_step": 0, "steps": [], "error": ""})
    ls = LoopState(_FakeDb(), task, _spec(), "无关目标", _user(), "trace-2")
    await graph.run_graph(ls)

    assert ls.task.status == AgentState.FAILED.value
    assert "未命中" in ls.checkpoint["error"]


async def test_graph_singleton_concurrent_runs_isolated() -> None:
    """模块级编译图单例并发驱动两个 run：状态各归各的宿主，互不串写。"""
    ls1 = LoopState(
        _FakeDb(), _task(_boundary_checkpoint("演示一")), _spec(), "演示一", _user(), "t1"
    )
    ls2 = LoopState(
        _FakeDb(), _task(_boundary_checkpoint("演示二")), _spec(), "演示二", _user(), "t2"
    )
    await asyncio.gather(graph.run_graph(ls1), graph.run_graph(ls2))

    assert ls1.task.status == AgentState.DONE.value
    assert ls2.task.status == AgentState.DONE.value
    assert ls1.checkpoint["goal"] == "演示一"
    assert ls2.checkpoint["goal"] == "演示二"
