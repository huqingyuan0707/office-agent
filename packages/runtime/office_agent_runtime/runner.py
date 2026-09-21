"""Run 主循环（编排层唯一执行位：提议 → executor.call → 观察 → 检查点 → 重复）

链路：api 受理 → start_run 建 Task 行（type="agent.run"）→ execute_run 主循环：
      RulePlanner 规划 → 逐步 executor.call（Scope 硬拦 / Schema 校验 / 熔断 / 审计
      全在内核，编排层绕不过）→ 每步写 RunStep → Task.checkpoint 随步推进 → 收敛。

关键决策（都有出处）：
- 受理与执行分离：单步失败（白名单越权 / 模板缺值 / executor 业务拒绝）把 run 收敛到
  FAILED 终态，但本循环不向外抛错——POST /runs 仍返回 code 0 的 run 概要，错误细节
  落在 RunStep 时间线与检查点里，由 GET /runs/{id} 轮询展示（前端轮询友好）。
- max_steps 硬顶：计划步数超出即 FAILED（1005），已执行步骤留痕，checkpoint 记录
  断点，续跑可从断点继续（防 LLM 规划器在 R1 出现循环烧 token 的地基）。
- 计划持久化：首次规划结果写进 checkpoint，续跑回放原计划（agent.yaml 中途变更
  也不会让步号与历史结果错位）；白名单仍按当前 spec 在执行前硬拦。
- 送审闭环口径：内核 executor.call 并不因 requires_approval 挂起——policy 只在判定
  结果里给出 approval_required 标记，真正落审批单由工具的本地实现负责（见 server
  审批模块口径「生成审批单由需审批的工具实现负责」）。因此 R0 的测试只覆盖直接执行
  路径；「送审 → run 挂起 WAITING_APPROVAL → 批准后 lazy resume」的闭环属 R2，
  届时复用本模块的检查点机制续跑。
- 状态机：全程走内核 TRANSITIONS（ensure_transition），编排层不私改状态；FAILED 可经
  resume 重新进入 PLANNING（内核口径），DONE 是唯一终态。
- 白名单：planner 提议的工具必须命中 AgentSpec 白名单（声明层已校验 rules ⊆ 白名单，
  这里是运行时最后防线，防 R1 的 LLM 规划器越权提议）；Scope 与角色可见性由内核
  policy 在 executor.call 里硬拦。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core import executor
from office_agent_core.contracts import AgentState, ToolContext, ensure_transition, state_label
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_runtime.models import RunStep
from office_agent_runtime.planner.rule import RulePlanner
from office_agent_runtime.spec import AgentSpec, PlannerStep
from office_agent_server.models import Task
from office_agent_server.rbac import CurrentUser

logger = logging.getLogger(__name__)

#: Run 复用 Task 表的类型标记（Task 无新增列，agent/goal/断点全在 checkpoint 里）
RUN_TASK_TYPE = "agent.run"

#: 步骤结果摘要的落库截断长度（完整结果在 checkpoint 里，时间线只展示摘要）
_DIGEST_LIMIT = 2000


def parse_checkpoint(raw: str) -> dict[str, Any]:
    """Task.checkpoint 列的 JSON 文本 → 字典（脏数据给空对象，不因断点坏拖垮读取）。"""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _dumps(value: Any) -> str:
    """JSON 文本落库（非可序列化对象降级为 str，绝不因落库失败丢留痕）。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def _digest(result: Any) -> str:
    """结果摘要（时间线展示用；截断加标记，完整结果在 checkpoint）。"""
    text = _dumps(result)
    return text if len(text) <= _DIGEST_LIMIT else text[:_DIGEST_LIMIT] + "…（已截断）"


def _move(task: Task, dst: AgentState) -> None:
    """沿内核状态机推进并把新状态落到 Task 行（非法流转 4009，编排层不私改状态）。"""
    task.status = str(ensure_transition(task.status, dst))


def _step_entries(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """检查点里的步骤条目（损坏时给空列表，读取路径绝不抛错）。"""
    entries = checkpoint.get("steps")
    return (
        [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
    )


def _record_entry(checkpoint: dict[str, Any], entry: dict[str, Any]) -> None:
    """按步号替换或追加步骤条目（重试成功覆盖失败条目），保持步号有序。"""
    entries = checkpoint.setdefault("steps", [])
    entries[:] = [item for item in entries if item.get("index") != entry["index"]]
    entries.append(entry)
    entries.sort(key=lambda item: int(item.get("index") or 0))


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
        input=_dumps({"agent": spec.name, "goal": goal}),
        checkpoint=_dumps(
            {"agent": spec.name, "goal": goal, "next_step": 0, "steps": [], "error": ""}
        ),
    )
    db.add(task)
    await db.flush()  # 先落行拿 id（后续 RunStep.run_id / 异步轮询都要用）
    return await execute_run(db, task=task, spec=spec, goal=goal, user=user, trace_id=trace_id)


def _plan_from_checkpoint(checkpoint: dict[str, Any]) -> list[PlannerStep] | None:
    """检查点里的持久化计划 → PlannerStep 列表；无计划返回 None（首次执行走规划）。

    续跑回放「原计划」而非按当前 agent.yaml 重规划：agent.yaml 在失败与续跑之间被
    修改时，重规划的步号与 checkpoint 回放的历史结果会错位，模板取值绑错工具；
    工具级白名单仍按当前 spec 在执行前硬拦（config 收窄不会被持久化计划绕过）。
    """
    raw = checkpoint.get("plan")
    if not isinstance(raw, list) or not raw:  # 空/损坏类型按「无计划」处理，走重新规划
        return None
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
    return steps


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
    # 断点恢复：已成功步骤的完整出参从 checkpoint 回放，供后续步骤的取值模板使用
    results: dict[int, dict[str, Any]] = {}
    for entry in _step_entries(checkpoint):
        if entry.get("status") != "ok" or not isinstance(entry.get("outcome"), dict):
            continue
        try:
            results[int(entry["index"])] = entry["outcome"]
        except (KeyError, TypeError, ValueError):
            continue
    try:
        index = int(checkpoint.get("next_step") or 0)
    except (TypeError, ValueError):
        index = 0

    def save_checkpoint() -> None:
        task.checkpoint = _dumps(checkpoint)

    def fail_run(reason: str) -> None:
        """run 级失败（未命中规则 / 超步数顶 / 未预期异常）：收敛 FAILED，不写幻影步骤行。"""
        checkpoint["error"] = reason
        task.error = _dumps({"message": reason})
        _move(task, AgentState.FAILED)
        save_checkpoint()

    def fail_step(index: int, tool: str, args: dict[str, Any], reason: str) -> None:
        """单步失败：该步置 failed（时间线可见）并把 run 收敛到 FAILED 终态。"""
        db.add(
            RunStep(
                tenant=user.tenant,
                run_id=str(task.id),
                step_index=index,
                tool=tool,
                args=_dumps(args),
                result_digest=reason,
                status="failed",
                trace_id=trace_id,
            )
        )
        _record_entry(
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
                args=_dumps(args),
                result_digest=_digest(outcome.get("result")),
                status="ok",
                approval_id=str(outcome.get("approval_id") or ""),
                trace_id=trace_id or str(outcome.get("trace_id") or ""),
            )
        )
        _record_entry(
            checkpoint, {"index": index, "tool": tool, "status": "ok", "outcome": outcome}
        )
        results[index] = outcome
        checkpoint["next_step"] = index + 1
        checkpoint["error"] = ""
        task.progress = round((index + 1) / total, 4) if total else 1.0
        save_checkpoint()

    try:
        _move(task, AgentState.PLANNING)
        # 计划持久化：首次执行时规划并写入 checkpoint，续跑回放原计划（防 agent.yaml
        # 中途变更导致步号与历史结果错位）；无持久化计划时按当前配置重新规划
        planned = _plan_from_checkpoint(checkpoint)
        if planned is None:
            planned = RulePlanner(spec).plan(goal)
            checkpoint["plan"] = [{"tool": s.tool, "args": s.args} for s in planned]
        if not planned:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"目标未命中智能体 {spec.name} 的任何规划规则，请调整目标表述或补充 agent.yaml 规则",
            )
        total = len(planned)
        _move(task, AgentState.ACTING)
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
            _move(task, AgentState.OBSERVING)
            try:
                outcome = await executor.call(ctx, name=step.tool, args=args, trace_id=trace_id)
            except BusinessError as exc:
                fail_step(index, step.tool, args, exc.msg)
                break
            _move(task, AgentState.REFLECTING)
            succeed_step(index, step.tool, args, outcome, total)
            index += 1
            if index < total:
                _move(task, AgentState.ACTING)
            else:
                _move(task, AgentState.DONE)
        else:
            # 循环未被 break 且还有剩余步（续跑边界：断点已在末尾）→ 直接收敛 DONE
            if task.status != AgentState.DONE.value:
                _move(task, AgentState.OBSERVING)
                _move(task, AgentState.REFLECTING)
                checkpoint["error"] = ""
                _move(task, AgentState.DONE)
                save_checkpoint()
    except BusinessError as exc:
        fail_run(exc.msg)
    except Exception as exc:  # 未预期异常同样收敛终态：受理接口保持 code 0，细节进检查点
        logger.exception("run %s 执行异常", task.id)
        fail_run(f"运行异常：{str(exc)[:200]}")

    steps_done = sum(1 for entry in _step_entries(checkpoint) if entry.get("status") == "ok")
    task.output = _dumps(
        {
            "agent": spec.name,
            "goal": goal,
            "status": task.status,
            "steps_done": steps_done,
            "error": checkpoint.get("error", ""),
        }
    )
    return {
        "run_id": task.id,
        "agent": spec.name,
        "goal": goal,
        "status": task.status,
        "status_label": state_label(task.status),
        "steps_done": steps_done,
        "error": checkpoint.get("error", ""),
    }
