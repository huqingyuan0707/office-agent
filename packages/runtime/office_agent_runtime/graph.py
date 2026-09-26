"""Run 主循环的 LangGraph StateGraph 装配（ADR-0005 阶段一：替换原 RunLoop.run 循环）

链路：runner.execute_run → run_graph(LoopState) → 图按「plan → act(自环) → END」
      驱动 loop_state.LoopState 的方法（prepare_plan / run_step），收敛后由
      runner 调 finish() 收尾。审批挂起=act 节点 route 到 END（条件边表达），
      续跑由 approvals.resolve_pending → execute_run 重建入参后重放 checkpoint。

口径（路 b 决策，见 ADR-0005 §2）：不挂 LangGraph checkpointer——
Task.checkpoint 是业务唯一真相源（前端轮询与审批中心的契约面），图编译产物
无状态、模块级单例复用；状态流转全走 checkpoint.move_state（内核 TRANSITIONS），
SQLite 锁纪律（落行即 commit / finalize 前 commit）在 runner 与 LoopState 侧保持。
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from office_agent_core.contracts import AgentState
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_runtime.checkpoint import move_state
from office_agent_runtime.loop_state import (
    LoopState,
    begin_acting,
    conclude_at_boundary,
    enter_executing,
)
from office_agent_runtime.spec import PlannerStep

logger = logging.getLogger(__name__)


class RunGraphState(TypedDict):
    """图内状态：只承载执行态宿主引用与路由标记，不做任何持久化（路 b）。"""

    ls: Any  # LoopState 实例（闭包式随状态流转，图本身无状态）
    planned: list[Any]  # prepare_plan 产出的 list[PlannerStep]
    route: str  # 节点间路由：act / again / stop


def _fail_stop(ls: LoopState, exc: Exception) -> dict[str, str]:
    """节点内未预期异常统一收口：run 级失败收敛终态后停图（受理与执行分离口径）。"""
    logger.exception("run %s 执行异常", ls.task.id)
    ls.fail_run(f"运行异常：{str(exc)[:200]}")
    return {"route": "stop"}


async def _plan_node(state: RunGraphState) -> dict[str, Any]:
    """规划节点：enter_executing → prepare_plan（回放或降级链）→ begin_acting。

    返回 route=act 进入执行；规划失败（BusinessError/未预期异常）收敛 FAILED 后 stop。
    """
    ls: LoopState = state["ls"]
    try:
        enter_executing(ls.task)
        planned = await ls.prepare_plan()
        ls.total = len(planned)
        begin_acting(ls.task)
    except BusinessError as exc:
        ls.fail_run(exc.msg)
        return {"route": "stop"}
    except Exception as exc:
        return _fail_stop(ls, exc)
    return {"planned": planned, "route": "act"}


async def _act_node(state: RunGraphState) -> dict[str, Any]:
    """单步执行节点（自环即原 while 循环）：边界收敛 → max_steps 硬顶 → run_step。

    route=again 继续下一步；stop 停图（审批挂起 / 单步失败 / 超步数 / 未预期异常 /
    续跑边界直达）。挂起与失败的区分由 task.status 承载，图不做分支。
    """
    ls: LoopState = state["ls"]
    planned: list[PlannerStep] = state["planned"]
    try:
        if ls.index >= ls.total:
            # 续跑边界（断点已在末尾）或末步后回环：直接收敛 DONE
            conclude_at_boundary(ls.task, ls.checkpoint, ls.save_checkpoint)
            return {"route": "stop"}
        if ls.index >= ls.spec.max_steps:
            raise BusinessError(
                ErrorCode.QUOTA_EXCEEDED,
                f"已达智能体 {ls.spec.name} 的最大步数上限 {ls.spec.max_steps}，"
                f"剩余 {ls.total - ls.index} 步未执行；可处理断点后显式续跑",
            )
        if not await ls.run_step(planned[ls.index]):
            return {"route": "stop"}
        ls.index += 1
        move_after_step = AgentState.ACTING if ls.index < ls.total else AgentState.DONE
        move_state(ls.task, move_after_step)
    except BusinessError as exc:
        ls.fail_run(exc.msg)
        return {"route": "stop"}
    except Exception as exc:
        return _fail_stop(ls, exc)
    return {"route": "again"}


def _route_stop_or(state: RunGraphState, cont: str) -> str:
    """条件边裁决：stop → END，否则走 cont 目标。"""
    return END if state["route"] == "stop" else cont


def _build_graph():  # type: ignore[no-untyped-def]
    """装配并编译 StateGraph（无 checkpointer，编译产物无状态可单例复用）。"""
    graph = StateGraph(RunGraphState)
    graph.add_node("plan", _plan_node)
    graph.add_node("act", _act_node)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges(
        "plan", lambda s: _route_stop_or(s, "act"), {"act": "act", END: END}
    )
    graph.add_conditional_edges("act", lambda s: _route_stop_or(s, "act"), {"act": "act", END: END})
    return graph.compile()


_GRAPH = _build_graph()


async def run_graph(ls: LoopState) -> None:
    """驱动一次 run 的图执行（不向外抛错：节点内部已把失败收敛到终态）。

    入参 ls 为已构造的 LoopState 宿主；recursion_limit 按 max_steps+2 放宽
    （plan 一步 + act 自环每步一步 + 边界收敛一步），防 LangGraph 默认 25 截断。
    """
    await _GRAPH.ainvoke(
        {"ls": ls, "planned": [], "route": ""},
        config={"recursion_limit": ls.spec.max_steps + 4},
    )
