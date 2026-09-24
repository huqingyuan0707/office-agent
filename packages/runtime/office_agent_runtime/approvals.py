"""审批闭环编排（R2）：送审分流挂起 / 内联裁决 / lazy resume 续跑

链路：runner 主循环每步在 executor.call 前查 spec.requires_approval →
      suspend_for_approval() 预检（Scope+Schema，与 invoke 端点同一分流口径，防
      「先过审批再被拒」）→ 复用 services.approval_flow.create_approval 落审批单 →
      RunStep 落 pending + checkpoint 存 pending_approval → run 挂起 WAITING_APPROVAL。
      查询 / 续跑路径经 resolve_pending() 内联查该审批结果：
      approved → 清挂起标记、记 approved_steps → 以发起人身份 execute_run 续跑
      （该步在主循环里按幂等口径重放、仍走 executor.call，时间线留完整出参）；
      rejected → 写 rejected RunStep + run 收敛 FAILED 终态留痕；
      pending → 维持挂起；不做回调推送（方案 §R2 明确 YAGNI）。

口径：
- 审批单、审批人身份、同人红线全在 server 侧 approval_flow（复用既有送审机制，
  编排层不复制）；decide_approval 在批准时的执行身份与 Scope 预检属既有闭环，
  run 时间线上的生效执行以本模块续跑重放位为准（工具声明 idempotent，重放安全）；
- 状态机全程内核 TRANSITIONS：挂起态 = WAITING_APPROVAL（非法流转 4009 由内核拦）；
- 断点即审批：checkpoint 同时存计划与 pending_approval，进程重启后裁决照常可续。
对齐：.trae/documents/智能体编排层实现方案.md §R2 / §3（审批挂起 → lazy resume）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core import policy, registry
from office_agent_core.contracts import AgentState, ToolSpec, validate_args
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_runtime import loader
from office_agent_runtime.checkpoint import (
    approved_steps,
    dumps,
    move_state,
    parse_checkpoint,
    record_entry,
    summarize_run,
)
from office_agent_runtime.models import RunStep
from office_agent_server.models import Approval, Task, User
from office_agent_server.rbac import CurrentUser
from office_agent_server.security import split_roles
from office_agent_server.services.approval_flow import create_approval

logger = logging.getLogger(__name__)


async def run_owner(db: AsyncSession, task: Task) -> CurrentUser:
    """运行发起人身份（续跑与重放以其本人角色执行——「以提交人身份执行」同一口径）。"""
    row = (
        await db.execute(
            select(User).where(User.tenant == task.tenant, User.username == task.username)
        )
    ).scalar_one_or_none()
    if row is None:
        raise BusinessError(
            ErrorCode.NOT_FOUND,
            f"运行发起人 {task.username} 不存在或已删除，无法以其身份续跑",
            404,
        )
    return CurrentUser(username=row.username, tenant=row.tenant, roles=split_roles(row.roles))


async def suspend_for_approval(
    db: AsyncSession,
    *,
    user: CurrentUser,
    task: Task,
    checkpoint: dict[str, Any],
    index: int,
    tool: str,
    args: dict[str, Any],
    planner_source: str,
    trace_id: str,
) -> dict[str, Any]:
    """送审分流：预检 → 落审批单 → RunStep pending → run 挂起 WAITING_APPROVAL。

    预检与 invoke 端点同一分流口径（Scope 硬拦 + Schema 校验先行，防「先过审批再被拒」）；
    预检不过抛 BusinessError，由 runner 收敛该步 failed。挂起即持久化：审批单跨请求
    存活必须落库，顺带把此前步骤留痕一并提交（与断点续跑口径一致）。
    """
    spec: ToolSpec = registry.get(tool)
    policy.ensure_allowed(roles=user.roles, spec=spec, args=args)
    errors = validate_args(spec.params, args)
    if errors:
        raise BusinessError(ErrorCode.PARAM_INVALID, "；".join(errors))

    approval = await create_approval(
        db,
        tenant=user.tenant,
        tool_name=tool,
        args=args,
        applicant=user.username,
        session_id=str(task.id),
        reason=f"智能体运行第 {index + 1} 步自动送审：{tool}",
        target=spec.approval_action or spec.name,
    )
    db.add(
        RunStep(
            tenant=user.tenant,
            run_id=str(task.id),
            step_index=index,
            tool=tool,
            args=dumps(args),
            result_digest=f"已提交审批，运行挂起：审批单 {approval.id}",
            status="pending",
            planner_source=planner_source,
            approval_id=approval.id,
            trace_id=trace_id,
        )
    )
    record_entry(
        checkpoint,
        {
            "index": index,
            "tool": tool,
            "status": "pending",
            "approval_id": approval.id,
            "message": "已提交审批，运行挂起，待复核员处理",
        },
    )
    checkpoint["pending_approval"] = {
        "approval_id": approval.id,
        "step_index": index,
        "tool": tool,
    }
    checkpoint["next_step"] = index  # 续跑从该步重放
    checkpoint["error"] = ""
    task.checkpoint = dumps(checkpoint)
    move_state(task, AgentState.WAITING_APPROVAL)
    logger.info(
        "run %s 第 %s 步送审挂起：approval_id=%s tool=%s", task.id, index, approval.id, tool
    )
    return {"approval_id": approval.id, "status": "pending", "step_index": index, "tool": tool}


async def resolve_pending(
    db: AsyncSession,
    *,
    task: Task,
    trace_id: str,
    execute_run: Callable[..., Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """挂起审批的统一裁决（GET 内联查询与显式续跑共用同一入口）。

    仅当 checkpoint 带 pending_approval 时行动：
    - approved → 清挂起标记 + 记 approved_steps → 以发起人身份续跑（execute_run 注入
      避免与 runner 循环依赖）；该步在主循环按幂等口径重放、仍走 executor.call；
    - rejected → 写 rejected RunStep + run 收敛 FAILED 终态留痕（幂等：终态后不重写）；
    - pending → 维持挂起（action=pending，调用方决定是否再轮询）；
    - 审批单缺失等脏态 → 收敛 FAILED（绝不永久卡死在挂起态）。
    其余情况 action=none（无挂起审批，调用方走原断点续跑路径）。
    """
    checkpoint = parse_checkpoint(task.checkpoint)
    pending = checkpoint.get("pending_approval")
    approval_id = str(pending.get("approval_id") or "").strip() if isinstance(pending, dict) else ""
    if not approval_id:
        return {"action": "none"}

    row = await _get_approval(db, task.tenant, approval_id)
    if task.status == AgentState.WAITING_APPROVAL.value:
        if row is None:
            return await _finalize_broken(
                db,
                task=task,
                checkpoint=checkpoint,
                reason=f"挂起审批单 {approval_id} 不存在或无权访问，无法继续；运行已收敛失败",
            )
        if row.status == "pending":
            return {
                "action": "pending",
                "approval_id": approval_id,
                "approval_status": "pending",
                "status_label": "待审批",
            }
        if row.status == "rejected":
            return await _finalize_rejected(
                db, task=task, checkpoint=checkpoint, row=row, pending=pending, trace_id=trace_id
            )
        return await _resume_approved(
            db,
            task=task,
            checkpoint=checkpoint,
            pending=pending,
            trace_id=trace_id,
            execute_run=execute_run,
        )
    if task.status == AgentState.FAILED.value and row is not None and row.status == "rejected":
        # 驳回终态的幂等读：不再重复留痕，供显式续跑入口拒绝（终态不可续）
        return {
            "action": "rejected",
            "already_final": True,
            "summary": summarize_run(task, checkpoint),
        }
    return {"action": "none"}


# ---------------- 内部助手 ----------------


async def _get_approval(db: AsyncSession, tenant: str, approval_id: str) -> Approval | None:
    """按租户取审批单（跨租户/不存在返回 None，裁决路径据此收敛失败而非 404）。"""
    row = (
        await db.execute(
            select(Approval).where(Approval.tenant == tenant, Approval.id == approval_id)
        )
    ).scalar_one_or_none()
    return row


async def _finalize_broken(
    db: AsyncSession,
    *,
    task: Task,
    checkpoint: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """挂起态脏数据（审批单缺失等）→ 收敛 FAILED，绝不永久卡死在挂起态。"""
    checkpoint["error"] = reason
    checkpoint.pop("pending_approval", None)
    task.error = dumps({"message": reason})
    task.checkpoint = dumps(checkpoint)
    move_state(task, AgentState.FAILED)
    logger.warning("run %s 挂起态脏数据收敛失败：%s", task.id, reason)
    return {"action": "failed", "summary": summarize_run(task, checkpoint)}


async def _finalize_rejected(
    db: AsyncSession,
    *,
    task: Task,
    checkpoint: dict[str, Any],
    row: Approval,
    pending: dict[str, Any],
    trace_id: str,
) -> dict[str, Any]:
    """驳回 → rejected RunStep 留痕 + run 收敛 FAILED（终态留痕，不删任何结果）。"""
    try:
        index = int(pending.get("step_index") or 0)
    except (TypeError, ValueError):
        index = 0
    tool = str(pending.get("tool") or row.action)
    reason = f"审批单 {row.id} 已被 {row.approver or '复核员'} 驳回：{(row.reason or '')[:120]}"
    db.add(
        RunStep(
            tenant=task.tenant,
            run_id=str(task.id),
            step_index=index,
            tool=tool,
            args=row.args or "{}",
            result_digest=f"审批驳回，运行终止：{reason}",
            status="rejected",
            planner_source=str(checkpoint.get("planner_source") or "rule"),
            approval_id=row.id,
            trace_id=trace_id,
        )
    )
    record_entry(
        checkpoint,
        {
            "index": index,
            "tool": tool,
            "status": "rejected",
            "approval_id": row.id,
            "message": reason,
        },
    )
    checkpoint["error"] = (
        f"第 {index + 1} 步工具 {tool} 的审批被驳回，运行终止"
        f"（如需重试请修改后重新发起运行）：{(row.reason or '')[:120]}"
    )
    task.error = dumps({"message": checkpoint["error"]})
    task.checkpoint = dumps(checkpoint)
    move_state(task, AgentState.FAILED)
    logger.info("run %s 审批驳回终态：approval_id=%s", task.id, row.id)
    return {"action": "rejected", "summary": summarize_run(task, checkpoint)}


async def _resume_approved(
    db: AsyncSession,
    *,
    task: Task,
    checkpoint: dict[str, Any],
    pending: dict[str, Any],
    trace_id: str,
    execute_run: Callable[..., Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """approved → 清挂起标记 + 记 approved_steps → 以发起人身份续跑主循环。

    该步由 runner 主循环按幂等口径重放（仍走 executor.call，时间线留完整出参与审计）；
    审批单号留在 pending 步的 RunStep 行上，时间线可对账。
    """
    try:
        index = int(pending.get("step_index") or 0)
    except (TypeError, ValueError):
        index = 0
    pre = approved_steps(checkpoint)
    if index not in pre:
        pre.append(index)
    checkpoint["approved_steps"] = pre
    checkpoint.pop("pending_approval", None)
    checkpoint["error"] = ""
    task.checkpoint = dumps(checkpoint)
    owner = await run_owner(db, task)
    agent_name = str(checkpoint.get("agent") or "")
    goal = str(checkpoint.get("goal") or "")
    summary = await execute_run(
        db,
        task=task,
        spec=loader.find_agent_spec(agent_name),
        goal=goal,
        user=owner,
        trace_id=trace_id,
    )
    return {"action": "resumed", "summary": summary}
