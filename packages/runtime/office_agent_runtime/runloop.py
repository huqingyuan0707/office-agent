"""Run 主循环执行态（runner.py 的拆分产物：循环骨架 + 单步治理分支 + 收敛收尾）

链路：runner.execute_run（薄装配，resolve_pending 注入契约）→ RunLoop.run() 跑
      「准备计划 → 逐步 run_step → 状态流转」→ RunLoop.finish() 做数值校验与概要合成。
      每步提议仍走 executor.call（Scope 硬拦 / 审批分流 / 熔断 / 审计全在内核，绕不过）。

口径与决策记录见 runner.py 模块 docstring（受理与执行分离 / max_steps 硬顶 /
计划持久化 / 状态机 / 白名单最后防线）——本文件只承载实现，决策唯一出处不复制两份。
对齐：.trae/documents/智能体编排层实现方案.md §3（运行实体与断点）、AGENTS.md §6
（execute_run 复杂度豁免的拆分偿还）。
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


class RunLoop:
    """一次 run 主循环的执行态（runner.execute_run 的拆分产物：状态集中，方法各自收敛复杂度）。

    职责边界：run() 只跑「规划 → 逐步执行 → 收敛」的循环骨架；单步治理分支
    （白名单 / 取值模板 / 送审挂起 / executor 调用）全在 run_step()；
    收尾（数值校验 + 概要合成）在 finish()。行为与原内联版逐分支等价。
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
        无持久化计划时按当前配置走降级链重规划。"""
        planned, src = _plan_from_checkpoint(self.checkpoint)
        if planned is None:
            planned, src = await _choose_and_plan(self.spec, self.goal)
            self.checkpoint["plan"] = [{"tool": s.tool, "args": s.args} for s in planned]
            self.checkpoint["planner_source"] = src
        self.planner_source = src
        if not planned:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"目标未命中智能体 {self.spec.name} 的任何规划规则，且 LLM 规划不可用",
            )
        return planned

    async def run_step(self, step: PlannerStep) -> bool:
        """执行断点处的单步；返回 False 表示主循环须立即停下（失败收敛或审批挂起）。"""
        index = self.index
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

    async def run(self) -> None:
        """主循环骨架：不向外抛错，失败也收敛终态（受理与执行分离，见 runner.py docstring）。"""
        try:
            _enter_executing(self.task)
            planned = await self.prepare_plan()
            self.total = len(planned)
            _begin_acting(self.task)
            while self.index < self.total:
                if self.index >= self.spec.max_steps:
                    raise BusinessError(
                        ErrorCode.QUOTA_EXCEEDED,
                        f"已达智能体 {self.spec.name} 的最大步数上限 {self.spec.max_steps}，"
                        f"剩余 {self.total - self.index} 步未执行；可处理断点后显式续跑",
                    )
                if not await self.run_step(planned[self.index]):
                    break
                self.index += 1
                move_state(
                    self.task,
                    AgentState.ACTING if self.index < self.total else AgentState.DONE,
                )
            else:
                _conclude_at_boundary(self.task, self.checkpoint, self.save_checkpoint)
        except BusinessError as exc:
            self.fail_run(exc.msg)
        except Exception as exc:  # 未预期异常同样收敛终态：受理接口保持 code 0，细节进检查点
            logger.exception("run %s 执行异常", self.task.id)
            self.fail_run(f"运行异常：{str(exc)[:200]}")

    async def finish(self) -> dict[str, Any]:
        """收敛收尾：R2 数值校验（降级不 500）+ 输出概要合成（不抛错，失败也走概要）。"""
        validation_block: dict[str, Any] = {}
        if self.task.status == AgentState.DONE.value:
            # 先提交步骤写入释放锁再进终答合成：finalize_answer 是 30s 级 LLM I/O，
            # 事务跨网络等待会把并发写者撞成 "database is locked"（同 start_run 口径）
            await self.db.commit()
            validation_block = await finalize_answer(
                spec=self.spec, goal=self.goal, results=self.results
            )
            self.checkpoint.update(validation_block)
            self.save_checkpoint()

        steps_done = sum(
            1 for entry in step_entries(self.checkpoint) if entry.get("status") == "ok"
        )
        self.task.output = dumps(
            {
                "agent": self.spec.name,
                "goal": self.goal,
                "status": self.task.status,
                "steps_done": steps_done,
                "error": self.checkpoint.get("error", ""),
                "answer": validation_block.get("answer", ""),
                "validation": validation_block.get("validation"),
            }
        )
        summary = summarize_run(self.task, self.checkpoint)
        if validation_block:
            summary["answer"] = validation_block.get("answer", "")
            summary["validation"] = validation_block.get("validation")
        return summary
