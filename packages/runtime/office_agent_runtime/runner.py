"""Run 主循环（编排层唯一执行位：提议 → executor.call → 观察 → 检查点 → 重复）

链路：api 受理 → start_run 建 Task 行（type="agent.run"）→ execute_run 主循环：
      规划 → 逐步 executor.call（Scope 硬拦 / Schema 校验 / 熔断 / 审计全在内核，
      编排层绕不过）→ 每步写 RunStep → Task.checkpoint 随步推进 → 收敛。

R2 增量（对齐 .trae/documents/智能体编排层实现方案.md §R2）：
- 送审分流：步骤命中 requires_approval → 不落 executor，经 approvals.suspend_for_approval
  预检（Scope+Schema，与 invoke 端点同一分流口径）并落审批单，run 挂起
  WAITING_APPROVAL（checkpoint 存 pending_approval）；批准后由 approvals.resolve_pending
  内联裁决 lazy resume——该步按幂等口径在主循环重放（approved_steps 标记跳过二次
  送审），重放仍走 executor.call，时间线留完整出参与审计。
- 回答数值校验：收敛 DONE 后经 validation.finalize_answer 合成 LLM 终答并校验数字
  溯源，标记随 checkpoint / run 概要透出（降级不 500，绝不阻断已完成步骤）。

关键决策（都有出处）：
- 受理与执行分离：单步失败（白名单越权 / 模板缺值 / executor 业务拒绝）把 run 收敛到
  FAILED 终态，但本循环不向外抛错——POST /runs 仍返回 code 0 的 run 概要，错误细节
  落在 RunStep 时间线与检查点里，由 GET /runs/{id} 轮询展示（前端轮询友好）。
- max_steps 硬顶：计划步数超出即 FAILED（1005），已执行步骤留痕，checkpoint 记录
  断点，续跑可从断点继续（防 LLM 规划器循环烧 token 的地基）。
- 计划持久化：首次规划结果写进 checkpoint，续跑回放原计划（agent.yaml 中途变更
  也不会让步号与历史结果错位）；白名单仍按当前 spec 在执行前硬拦。
- 状态机：全程走内核 TRANSITIONS（ensure_transition），编排层不私改状态；FAILED 可经
  resume 重新进入 PLANNING（内核口径），DONE 是唯一成功终态；审批解除的重入从
  WAITING_APPROVAL 直进 ACTING（内核口径），计划回放照常、不重复送审。
- 白名单：planner 提议的工具必须命中 AgentSpec 白名单（声明层已校验 rules ⊆ 白名单，
  这里是运行时最后防线，防 LLM 规划器越权提议）；Scope 与角色可见性由内核 policy
  在 executor.call 里硬拦。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core import executor, registry
from office_agent_core.contracts import AgentState, ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_runtime import approvals
from office_agent_runtime.checkpoint import (
    approved_steps,
    dumps,
    move_state,
    next_step_of,
    parse_checkpoint,
    record_entry,
    step_entries,
    summarize_run,
)
from office_agent_runtime.models import RunStep
from office_agent_runtime.planner.llm import LlmFunctionCallPlanner, LlmPlanError
from office_agent_runtime.planner.rule import RulePlanner
from office_agent_runtime.spec import AgentSpec, PlannerStep
from office_agent_runtime.validation import finalize_answer
from office_agent_server.models import Task
from office_agent_server.rbac import CurrentUser

logger = logging.getLogger(__name__)

#: Run 复用 Task 表的类型标记（Task 无新增列，agent/goal/断点全在 checkpoint 里）
RUN_TASK_TYPE = "agent.run"

#: 步骤结果摘要的落库截断长度（完整结果在 checkpoint 里，时间线只展示摘要）
_DIGEST_LIMIT = 2000


def _digest(result: Any) -> str:
    """结果摘要（时间线展示用；截断加标记，完整结果在 checkpoint）。"""
    text = dumps(result)
    return text if len(text) <= _DIGEST_LIMIT else text[:_DIGEST_LIMIT] + "…（已截断）"


def _needs_approval(tool_spec: ToolSpec | None, index: int, pre_approved: list[int]) -> bool:
    """该步是否需要送审挂起（已在 approved_steps 里的步按幂等口径重放，不再送审）。"""
    return tool_spec is not None and tool_spec.requires_approval and index not in pre_approved


def _replay_results(checkpoint: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """断点恢复：已成功步骤的完整出参从 checkpoint 回放（取值模板与数值校验的地基）。"""
    results: dict[int, dict[str, Any]] = {}
    for entry in step_entries(checkpoint):
        if entry.get("status") != "ok" or not isinstance(entry.get("outcome"), dict):
            continue
        try:
            results[int(entry["index"])] = entry["outcome"]
        except (KeyError, TypeError, ValueError):
            continue
    return results


def _enter_executing(task: Task) -> None:
    """进入执行前状态：常规路径走 PLANNING（重规划或回放计划）；审批解除的重入从
    WAITING_APPROVAL 直进 ACTING（内核口径），已在 ACTING 的不重复流转。"""
    if task.status == AgentState.WAITING_APPROVAL.value:
        move_state(task, AgentState.ACTING)
    elif task.status != AgentState.ACTING.value:
        move_state(task, AgentState.PLANNING)


def _begin_acting(task: Task) -> None:
    """规划落定后进入执行态（审批解除的重入已在 ACTING，不重复流转）。"""
    if task.status != AgentState.ACTING.value:
        move_state(task, AgentState.ACTING)


def _conclude_at_boundary(task: Task, checkpoint: dict[str, Any], save: Callable[[], None]) -> None:
    """续跑边界（断点已在末尾）：循环体未再执行，直接收敛 DONE。"""
    if task.status != AgentState.DONE.value:
        move_state(task, AgentState.OBSERVING)
        move_state(task, AgentState.REFLECTING)
        checkpoint["error"] = ""
        move_state(task, AgentState.DONE)
        save()


def _plan_from_checkpoint(checkpoint: dict[str, Any]) -> tuple[list[PlannerStep] | None, str]:
    """检查点里的持久化计划 → (PlannerStep 列表, planner_source) 元组。

    返回 (None, "") 表示无持久化计划；损坏的 plan 给 None 让调用方重规划。
    续跑回放「原计划 + 原来源」而非按当前 agent.yaml 重规划：agent.yaml 在
    失败与续跑之间被修改时，重规划的步号与 checkpoint 回放的历史结果会错位；
    工具级白名单仍按当前 spec 在执行前硬拦。
    """
    raw = checkpoint.get("plan")
    src = str(checkpoint.get("planner_source") or "").strip() or "rule"
    if not isinstance(raw, list) or not raw:
        return None, ""
    steps: list[PlannerStep] = []
    for entry in raw:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("tool"), str)
            or not isinstance(entry.get("args"), dict)
        ):
            raise BusinessError(
                ErrorCode.PARAM_INVALID, "检查点中的计划数据损坏，无法续跑；请重新发起运行"
            )
        steps.append(PlannerStep(tool=entry["tool"], args=entry["args"]))
    return steps, src


async def _choose_and_plan(spec: AgentSpec, goal: str) -> tuple[list[PlannerStep], str]:
    """降级链：LLM 优先 → 失败降 Rule → 无规则报错（不静默编造）。

    返回 (planned_steps, planner_source)；source 是 "llm" 或 "rule"。
    LLM 出站客户端只在本次规划内使用，finally 里关闭（自建连接不跨调用滞留）。
    """
    if spec.llm:
        planner = LlmFunctionCallPlanner.from_spec(spec)
        try:
            steps = await planner.plan(goal)
        except LlmPlanError as exc:
            if not spec.rules:
                raise BusinessError(
                    ErrorCode.LLM_FAILED,
                    f"智能体 {spec.name} 指定了 LLM 规划（profile={spec.llm}）"
                    f"但 LLM 不可用，且未配置 rules 规则规划，无法执行：{exc.msg}",
                ) from None
            logger.warning(
                "LLM planner 规划失败：%s（agent=%s），降级 RulePlanner", exc.msg, spec.name
            )
        else:
            if steps:
                return steps, "llm"
            # LLM 返回空（无 tool_calls）：视为不可用，降级规则规划
            logger.warning("LLM planner 未返回 tool_calls（agent=%s），降级 RulePlanner", spec.name)
        finally:
            await planner.aclose()
    if spec.rules:
        return RulePlanner(spec).plan(goal), "rule"
    raise BusinessError(
        ErrorCode.PARAM_INVALID,
        f"智能体 {spec.name} 未配置 llm 也未配置 rules，无法规划",
    )


async def start_run(
    db: AsyncSession,
    *,
    spec: AgentSpec,
    goal: str,
    user: CurrentUser,
    trace_id: str,
) -> dict[str, Any]:
    """发起一次运行：建 Task 行（type="agent.run"）并同步执行主循环。

    Task 表没有的列（agent / goal / 断点）全存 checkpoint 与 input，不硬塞；
    发起人就在 Task.username 既有列上。
    """
    task = Task(
        tenant=user.tenant,
        username=user.username,
        type=RUN_TASK_TYPE,
        status=AgentState.IDLE.value,
        input=dumps({"agent": spec.name, "goal": goal}),
        checkpoint=dumps(
            {"agent": spec.name, "goal": goal, "next_step": 0, "steps": [], "error": ""}
        ),
    )
    db.add(task)
    await db.flush()  # 先落行拿 id（后续 RunStep.run_id / 异步轮询都要用）
    return await execute_run(db, task=task, spec=spec, goal=goal, user=user, trace_id=trace_id)


async def execute_run(
    db: AsyncSession,
    *,
    task: Task,
    spec: AgentSpec,
    goal: str,
    user: CurrentUser,
    trace_id: str,
) -> dict[str, Any]:
    """执行一次 run 主循环，返回 run 概要（不抛错，失败也走概要——见模块 docstring）。"""
    ctx = ToolContext(
        db=db,
        tenant=user.tenant,
        username=user.username,
        roles=list(user.roles),
        trace_id=trace_id,
    )
    checkpoint = parse_checkpoint(task.checkpoint)
    checkpoint.setdefault("agent", spec.name)
    checkpoint["goal"] = goal
    results = _replay_results(checkpoint)
    index = next_step_of(checkpoint)
    pre_approved = approved_steps(checkpoint)

    def save_checkpoint() -> None:
        task.checkpoint = dumps(checkpoint)

    def fail_run(reason: str) -> None:
        """run 级失败（未命中规则 / 超步数顶 / 未预期异常）：收敛 FAILED，不写幻影步骤行。"""
        checkpoint["error"] = reason
        task.error = dumps({"message": reason})
        move_state(task, AgentState.FAILED)
        save_checkpoint()

    def fail_step(index: int, tool: str, args: dict[str, Any], reason: str) -> None:
        """单步失败：该步置 failed（时间线可见）并把 run 收敛到 FAILED 终态。"""
        db.add(
            RunStep(
                tenant=user.tenant,
                run_id=str(task.id),
                step_index=index,
                tool=tool,
                args=dumps(args),
                result_digest=reason,
                status="failed",
                planner_source=planner_source,
                trace_id=trace_id,
            )
        )
        record_entry(
            checkpoint, {"index": index, "tool": tool, "status": "failed", "message": reason}
        )
        checkpoint["next_step"] = index  # 断点停在本步：续跑重试同一行
        fail_run(reason)

    def succeed_step(
        index: int, tool: str, args: dict[str, Any], outcome: dict[str, Any], total: int
    ) -> None:
        """单步成功：写时间线行 + 检查点推进（前端轮询与续跑的地基）。"""
        db.add(
            RunStep(
                tenant=user.tenant,
                run_id=str(task.id),
                step_index=index,
                tool=tool,
                args=dumps(args),
                result_digest=_digest(outcome.get("result")),
                status="ok",
                planner_source=planner_source,
                approval_id=str(outcome.get("approval_id") or ""),
                trace_id=trace_id or str(outcome.get("trace_id") or ""),
            )
        )
        record_entry(checkpoint, {"index": index, "tool": tool, "status": "ok", "outcome": outcome})
        results[index] = outcome
        checkpoint["next_step"] = index + 1
        checkpoint["error"] = ""
        task.progress = round((index + 1) / total, 4) if total else 1.0
        save_checkpoint()

    try:
        _enter_executing(task)
        # 计划持久化：首次执行时规划并写入 checkpoint（含 planner_source），
        # 续跑回放原计划 + 原来源（防 agent.yaml 中途变更导致步号与历史结果错位）；
        # 无持久化计划时按当前配置走降级链重规划
        planned, planner_source = _plan_from_checkpoint(checkpoint)
        if planned is None:
            planned, planner_source = await _choose_and_plan(spec, goal)
            checkpoint["plan"] = [{"tool": s.tool, "args": s.args} for s in planned]
            checkpoint["planner_source"] = planner_source
        if not planned:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"目标未命中智能体 {spec.name} 的任何规划规则，且 LLM 规划不可用",
            )
        total = len(planned)
        _begin_acting(task)
        while index < total:
            if index >= spec.max_steps:
                raise BusinessError(
                    ErrorCode.QUOTA_EXCEEDED,
                    f"已达智能体 {spec.name} 的最大步数上限 {spec.max_steps}，"
                    f"剩余 {total - index} 步未执行；可处理断点后显式续跑",
                )
            step = planned[index]
            if step.tool not in spec.tools:
                fail_step(
                    index,
                    step.tool,
                    step.args,
                    f"工具 {step.tool} 不在智能体 {spec.name} 的白名单内，已拒绝执行"
                    "（planner 只能提议白名单内的工具）",
                )
                break
            try:
                args = RulePlanner.resolve_args(step.args, results)
            except BusinessError as exc:
                fail_step(index, step.tool, step.args, exc.msg)
                break
            tool_spec = registry.maybe_get(step.tool)
            if _needs_approval(tool_spec, index, pre_approved):
                # R2 送审分流：需审批工具不落 executor——落审批单并挂起，等待裁决后续跑
                try:
                    await approvals.suspend_for_approval(
                        db,
                        user=user,
                        task=task,
                        checkpoint=checkpoint,
                        index=index,
                        tool=step.tool,
                        args=args,
                        planner_source=planner_source,
                        trace_id=trace_id,
                    )
                except BusinessError as exc:
                    fail_step(index, step.tool, args, exc.msg)
                    break
                break  # 挂起即返回：等待审批中心批/驳，经 resolve_pending 续跑
            move_state(task, AgentState.OBSERVING)
            try:
                outcome = await executor.call(ctx, name=step.tool, args=args, trace_id=trace_id)
            except BusinessError as exc:
                fail_step(index, step.tool, args, exc.msg)
                break
            move_state(task, AgentState.REFLECTING)
            succeed_step(index, step.tool, args, outcome, total)
            index += 1
            if index < total:
                move_state(task, AgentState.ACTING)
            else:
                move_state(task, AgentState.DONE)
        else:
            _conclude_at_boundary(task, checkpoint, save_checkpoint)
    except BusinessError as exc:
        fail_run(exc.msg)
    except Exception as exc:  # 未预期异常同样收敛终态：受理接口保持 code 0，细节进检查点
        logger.exception("run %s 执行异常", task.id)
        fail_run(f"运行异常：{str(exc)[:200]}")

    # R2 回答数值校验：收敛 DONE 后合成 LLM 终答并标注（降级不 500，绝不阻断已完成步骤）
    validation_block: dict[str, Any] = {}
    if task.status == AgentState.DONE.value:
        validation_block = await finalize_answer(spec=spec, goal=goal, results=results)
        checkpoint.update(validation_block)
        save_checkpoint()

    steps_done = sum(1 for entry in step_entries(checkpoint) if entry.get("status") == "ok")
    task.output = dumps(
        {
            "agent": spec.name,
            "goal": goal,
            "status": task.status,
            "steps_done": steps_done,
            "error": checkpoint.get("error", ""),
            "answer": validation_block.get("answer", ""),
            "validation": validation_block.get("validation"),
        }
    )
    summary = summarize_run(task, checkpoint)
    if validation_block:
        summary["answer"] = validation_block.get("answer", "")
        summary["validation"] = validation_block.get("validation")
    return summary
