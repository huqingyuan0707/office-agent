"""一次 run 的执行态宿主（LoopState：runloop.py 的 LangGraph 迁移改造产物）

链路：graph.py 的 StateGraph 节点闭包持有本类实例——plan 节点调 prepare_plan、
      act 节点调 run_step（白名单 / 取值模板 / 送审分流 / executor.call），
      图收敛后由 runner.execute_run 调 finish() 做数值校验与概要合成。
      原 RunLoop.run() 的 while 循环由 graph.py 条件边替代（ADR-0005 阶段一）。

口径与决策记录见 runner.py 模块 docstring（受理与执行分离 / max_steps 硬顶 /
计划持久化 / 状态机 / 白名单最后防线）——本文件只承载执行态实现；
checkpoint JSON 形状、状态流转（move_state → 内核 TRANSITIONS）与 SQLite 锁纪律
（finish 进 finalize_answer 前先 commit）与原 runloop.py 逐点一致。
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
)
from office_agent_runtime.models import RunStep
from office_agent_runtime.planner.llm import (
    LlmFunctionCallPlanner,
    LlmPlanError,
    profile_configured,
)
from office_agent_runtime.planner.react import ReACTPlanner
from office_agent_runtime.planner.rule import RulePlanner
from office_agent_runtime.spec import AgentSpec, PlannerStep, compose_context_goal
from office_agent_runtime.validation import finalize_answer as finalize_answer
from office_agent_server.models import Task
from office_agent_server.rbac import CurrentUser

logger = logging.getLogger(__name__)

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
    """断点恢复：已成功步骤的完整出参从 checkpoint 回放（取值模板与数值校验的地基）。

    入参 checkpoint 为解析后的检查点字典；返回 {步号: 完整出参}。
    无 steps 条目或条目缺 status=ok / outcome 时跳过该条（不抛异常）。
    """
    results: dict[int, dict[str, Any]] = {}
    for entry in step_entries(checkpoint):
        if entry.get("status") != "ok" or not isinstance(entry.get("outcome"), dict):
            continue
        try:
            results[int(entry["index"])] = entry["outcome"]
        except (KeyError, TypeError, ValueError):
            continue
    return results


def enter_executing(task: Task) -> None:
    """进入执行前状态：常规路径走 PLANNING（重规划或回放计划）；审批解除的重入从
    WAITING_APPROVAL 直进 ACTING（内核口径），已在 ACTING 的不重复流转。"""
    if task.status == AgentState.WAITING_APPROVAL.value:
        move_state(task, AgentState.ACTING)
    elif task.status != AgentState.ACTING.value:
        move_state(task, AgentState.PLANNING)


def begin_acting(task: Task) -> None:
    """规划落定后进入执行态（审批解除的重入已在 ACTING，不重复流转）。"""
    if task.status != AgentState.ACTING.value:
        move_state(task, AgentState.ACTING)


def conclude_at_boundary(task: Task, checkpoint: dict[str, Any], save: Callable[[], None]) -> None:
    """续跑边界（断点已在末尾）：图未再执行任何步骤，直接收敛 DONE（已在 DONE 不重复流转）。"""
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
    """降级链：plan 模式 LLM 优先 → 失败降 Rule → 无规则报错（不静默编造）。

    react 模式（混合串联）：规则快路径优先（关键词命中走确定性链，链内文档步
    已切大模型成稿）；未中且 LLM 已配置 → 返回 ([], "react") 空计划起步，步骤由
    LoopState.plan_next 逐步增量补；profile 未配置在受理即报错（受理与执行分离，
    不让用户等一轮执行才看到失败）。
    返回 (planned_steps, planner_source)；source 是 "llm" / "rule" / "react"。
    LLM 出站客户端只在本次规划内使用，finally 里关闭（自建连接不跨调用滞留）。
    """
    if spec.llm_mode == "react":
        if spec.rules:
            steps = RulePlanner(spec).plan(goal)
            if steps:
                return steps, "rule"
        if not spec.llm:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"智能体 {spec.name} 的 llm_mode=react 但未配置 llm，"
                f"且目标未命中任何规则，无法执行",
            )
        if not profile_configured(spec.llm):
            raise BusinessError(
                ErrorCode.LLM_FAILED,
                f"智能体 {spec.name} 的 llm_mode=react 且目标未命中规则，"
                f"但 LLM profile「{spec.llm}」未在 LLM_PROVIDERS 配置，无法执行",
            )
        return [], "react"
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


class LoopState:
    """一次 run 主循环的执行态宿主（原 RunLoop 类去掉 run() 循环骨架后的全部成员）。

    职责边界：graph.py 的节点驱动本类——plan 节点调 prepare_plan，act 节点调
    run_step（每步治理分支：白名单 / 取值模板 / 送审挂起 / executor.call 全在此），
    收尾调 finish()（数值校验 + 概要合成）。行为与原 RunLoop 逐分支等价。
    """

    def __init__(
        self,
        db: AsyncSession,
        task: Task,
        spec: AgentSpec,
        goal: str,
        user: CurrentUser,
        trace_id: str,
    ) -> None:
        self.db = db
        self.task = task
        self.spec = spec
        self.goal = goal
        self.user = user
        self.trace_id = trace_id
        self.ctx = ToolContext(
            db=db,
            tenant=user.tenant,
            username=user.username,
            roles=list(user.roles),
            trace_id=trace_id,
        )
        self.checkpoint = parse_checkpoint(task.checkpoint)
        self.checkpoint.setdefault("agent", spec.name)
        self.checkpoint["goal"] = goal
        # 会话多轮上下文（checkpoint 持久化，续跑回放注入规划；口径同 spec.compose_context_goal）
        self.goal_for_plan = compose_context_goal(str(self.checkpoint.get("context") or ""), goal)
        self.results = _replay_results(self.checkpoint)
        self.index = next_step_of(self.checkpoint)
        self.pre_approved = approved_steps(self.checkpoint)
        self.planner_source = ""
        self.total = 0

    def save_checkpoint(self) -> None:
        self.task.checkpoint = dumps(self.checkpoint)

    def fail_run(self, reason: str) -> None:
        """run 级失败（未命中规则 / 超步数顶 / 未预期异常）：收敛 FAILED，不写幻影步骤行。"""
        self.checkpoint["error"] = reason
        self.task.error = dumps({"message": reason})
        move_state(self.task, AgentState.FAILED)
        self.save_checkpoint()

    def fail_step(self, index: int, tool: str, args: dict[str, Any], reason: str) -> None:
        """单步失败：该步置 failed（时间线可见）并把 run 收敛到 FAILED 终态。"""
        self.db.add(
            RunStep(
                tenant=self.user.tenant,
                run_id=str(self.task.id),
                step_index=index,
                tool=tool,
                args=dumps(args),
                result_digest=reason,
                status="failed",
                planner_source=self.planner_source,
                trace_id=self.trace_id,
            )
        )
        record_entry(
            self.checkpoint, {"index": index, "tool": tool, "status": "failed", "message": reason}
        )
        self.checkpoint["next_step"] = index  # 断点停在本步：续跑重试同一行
        self.fail_run(reason)

    def succeed_step(
        self, index: int, tool: str, args: dict[str, Any], outcome: dict[str, Any]
    ) -> None:
        """单步成功：写时间线行 + 检查点推进（前端轮询与续跑的地基）。"""
        self.db.add(
            RunStep(
                tenant=self.user.tenant,
                run_id=str(self.task.id),
                step_index=index,
                tool=tool,
                args=dumps(args),
                result_digest=_digest(outcome.get("result")),
                status="ok",
                planner_source=self.planner_source,
                approval_id=str(outcome.get("approval_id") or ""),
                trace_id=self.trace_id or str(outcome.get("trace_id") or ""),
            )
        )
        record_entry(
            self.checkpoint, {"index": index, "tool": tool, "status": "ok", "outcome": outcome}
        )
        self.results[index] = outcome
        self.checkpoint["next_step"] = index + 1
        self.checkpoint["error"] = ""
        self.task.progress = round((index + 1) / self.total, 4) if self.total else 1.0
        self.save_checkpoint()

    async def prepare_plan(self) -> list[PlannerStep]:
        """计划持久化：首次执行时规划并写入 checkpoint（含 planner_source），
        续跑回放原计划 + 原来源（防 agent.yaml 中途变更导致步号与历史结果错位）；
        无持久化计划时按当前配置走降级链重规划。计划为空时抛 BusinessError
        （react 起步除外——空计划交 plan_next 逐步增量补）。"""
        planned, src = _plan_from_checkpoint(self.checkpoint)
        if planned is None:
            planned, src = await _choose_and_plan(self.spec, self.goal_for_plan)
            self.checkpoint["plan"] = [{"tool": s.tool, "args": s.args} for s in planned]
            self.checkpoint["planner_source"] = src
        self.planner_source = src
        if not planned:
            if src == "react":
                return []
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"目标未命中智能体 {self.spec.name} 的任何规划规则，且 LLM 规划不可用",
            )
        return planned

    async def plan_next(self) -> PlannerStep | None:
        """react 逐步再规划：LLM 看全部已完成步骤真实出参提议下一步。

        增量追加 checkpoint["plan"]（条目与既有计划同形 {tool, args}，断点续跑经
        _plan_from_checkpoint 回放不漂移）；LLM 判定收工返回 None（不追加）；
        出站失败抛 LlmPlanError，由 graph 的边界分支决定失败收敛或带产出收敛。

        出站前 commit（正常为 no-op）：防 session 残留 pending 写跨 LLM 长 I/O 持锁。
        """
        await self.db.commit()
        planner = ReACTPlanner.from_spec(self.spec)
        try:
            step = await planner.next_step(self.goal_for_plan, self.results)
        finally:
            await planner.aclose()
        if step is None:
            return None
        plan = self.checkpoint.setdefault("plan", [])
        plan.append({"tool": step.tool, "args": step.args})
        self.total = len(plan)
        self.save_checkpoint()
        return step

    async def run_step(self, step: PlannerStep) -> bool:
        """执行断点处的单步；返回 False 表示主循环须立即停下（失败收敛或审批挂起）。

        出口统一 commit（SQLite 锁纪律，同 start_run/finish 口径）：步骤时间线与
        检查点落库即释放写锁——下一步的 executor.call（LLM 成稿 60s+ 级长 I/O）
        执行期间 session 不持 pending 写，并发 run/审批重放不再撞 "database is locked"
        （2026-09-26 冒烟实测：pending RunStep 经 autoflush 拿写锁跨 120s 工具重试）。
        """
        index = self.index
        try:
            return await self._run_step_inner(step, index)
        finally:
            await self.db.commit()

    async def _run_step_inner(self, step: PlannerStep, index: int) -> bool:
        """run_step 主体（commit 由外层 finally 统一负责）。"""
        if step.tool not in self.spec.tools:
            self.fail_step(
                index,
                step.tool,
                step.args,
                f"工具 {step.tool} 不在智能体 {self.spec.name} 的白名单内，已拒绝执行"
                "（planner 只能提议白名单内的工具）",
            )
            return False
        try:
            args = RulePlanner.resolve_args(step.args, self.results)
        except BusinessError as exc:
            self.fail_step(index, step.tool, step.args, exc.msg)
            return False
        tool_spec = registry.maybe_get(step.tool)
        if _needs_approval(tool_spec, index, self.pre_approved):
            # R2 送审分流：需审批工具不落 executor——落审批单并挂起，等待裁决后续跑
            try:
                await approvals.suspend_for_approval(
                    self.db,
                    user=self.user,
                    task=self.task,
                    checkpoint=self.checkpoint,
                    index=index,
                    tool=step.tool,
                    args=args,
                    planner_source=self.planner_source,
                    trace_id=self.trace_id,
                )
            except BusinessError as exc:
                self.fail_step(index, step.tool, args, exc.msg)
                return False
            return False  # 挂起即返回：等待审批中心批/驳，经 resolve_pending 续跑
        move_state(self.task, AgentState.OBSERVING)
        try:
            outcome = await executor.call(
                self.ctx, name=step.tool, args=args, trace_id=self.trace_id
            )
        except BusinessError as exc:
            self.fail_step(index, step.tool, args, exc.msg)
            return False
        move_state(self.task, AgentState.REFLECTING)
        self.succeed_step(index, step.tool, args, outcome)
        return True

    async def finish(self) -> dict[str, Any]:
        """收敛收尾（实现见 run_finish.finish_run：数值校验 + 执行链路 + 概要合成）。"""
        from office_agent_runtime.run_finish import finish_run

        return await finish_run(self)
