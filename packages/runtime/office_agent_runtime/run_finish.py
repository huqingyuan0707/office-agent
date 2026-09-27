"""run 收敛收尾（LoopState.finish 的实现体，对齐 PRD §4.3 执行链路追踪）

链路：runner.execute_run / approvals.resolve_pending → LoopState.finish（薄委托）
  → 本模块 finish_run：R2 数值校验 → 执行链路组装 → Task.output 落库 → run 概要。

拆分原因：loop_state.py 触体量红线（FILE_LINE_BUDGET 427），收尾逻辑独立成篇；
  finalize_answer 经 loop_state 模块属性调用（test_graph 的桩点保持不变）。
红线：trace 存 checkpoint/output 内嵌 JSON 不新增表列；summary 不带 trace
  （POST 受理返回经 summary，trace 只在 GET /runs/{id} 对管理员透出）。
"""

from __future__ import annotations

from typing import Any

from office_agent_core.contracts import AgentState
from office_agent_runtime import loop_state as loop_state_module
from office_agent_runtime.checkpoint import dumps, step_entries, summarize_run
from office_agent_runtime.trace import build_run_trace, input_from_state


async def finish_run(state: Any) -> dict[str, Any]:
    """收敛收尾：R2 数值校验（降级不 500）+ 执行链路组装 + 输出概要合成（不抛错，失败也走概要）。

    state 取 LoopState 鸭子类型（task/db/spec/goal/planner_source/results/checkpoint）；
    终答合成走 loop_state.finalize_answer 模块属性（单测桩点唯一出处）。
    """
    validation_block: dict[str, Any] = {}
    if state.task.status == AgentState.DONE.value:
        # 先提交步骤写入释放锁再进终答合成：finalize_answer 是 30s 级 LLM I/O，
        # 事务跨网络等待会把并发写者撞成 "database is locked"（同 start_run 口径）
        await state.db.commit()
        validation_block = await loop_state_module.finalize_answer(
            spec=state.spec,
            goal=state.goal,
            results=state.results,
            planner_source=state.planner_source,
        )
        state.checkpoint.update(validation_block)
        state.save_checkpoint()

    # 执行链路：终态统一组装（成功/失败照常留痕可查），组装本身绝不抛错
    trace = build_run_trace(input_from_state(state, validation_block))
    state.checkpoint["trace"] = trace
    state.save_checkpoint()

    steps_done = sum(1 for entry in step_entries(state.checkpoint) if entry.get("status") == "ok")
    state.task.output = dumps(
        {
            "agent": state.spec.name,
            "goal": state.goal,
            "status": state.task.status,
            "steps_done": steps_done,
            "error": state.checkpoint.get("error", ""),
            "answer": validation_block.get("answer", ""),
            "validation": validation_block.get("validation"),
            "trace": trace,
        }
    )
    summary = summarize_run(state.task, state.checkpoint)
    if validation_block:
        summary["answer"] = validation_block.get("answer", "")
        summary["validation"] = validation_block.get("validation")
    return summary
