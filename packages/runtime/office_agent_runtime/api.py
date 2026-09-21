"""智能体运行时端点（清单 / 发起运行 / 时间线 / 断点续跑）

链路：GET /agents（清单）→ POST /runs（受理即跑，code 0 返回概要）
      → GET /runs/{id}（状态 + RunStep 时间线，前端轮询）
      → POST /runs/{id}/resume（checkpoint 续跑；R2 起审批通过后也可显式触发）。

口径：
- 薄封装红线：鉴权复用壳层 get_current_user；租户隔离与审批中心同口径（tenant 级可见）；
  治理口径全在内核 executor，端点不复制。
- 受理与执行分离：创建运行永远 code 0 返回 run 概要，执行失败在时间线里（见 runner）。
- trace 红线：trace 取入站 ``X-Trace-Id``（middleware.current_trace_id），
  每步 RunStep 落同一条 trace，跨系统联动时两侧审计可对账。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core.contracts import AgentState, state_label
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_runtime import loader
from office_agent_runtime.models import RunStep
from office_agent_runtime.runner import RUN_TASK_TYPE, execute_run, parse_checkpoint, start_run
from office_agent_server.db import get_db
from office_agent_server.middleware import current_trace_id
from office_agent_server.models import Task
from office_agent_server.rbac import CurrentUser, get_current_user
from office_agent_server.responses import ok

router = APIRouter(tags=["runtime"])


class CreateRunRequest(BaseModel):
    """发起运行入参（端点私有 DTO）。"""

    agent: str = Field(min_length=1, max_length=64)
    goal: str = Field(min_length=1, max_length=500)


def _load_json(text: str, fallback: Any) -> Any:
    """JSON 文本出参（解析失败原样返回文本，脏数据不拖垮时间线渲染）。"""
    try:
        return json.loads(text) if text else fallback
    except ValueError:
        return text


def _step_view(row: RunStep) -> dict[str, Any]:
    """步骤时间线出参（args / result 解析回对象，前端直接渲染）。"""
    return {
        "step_index": row.step_index,
        "tool": row.tool,
        "status": row.status,
        "args": _load_json(row.args, {}),
        "result": _load_json(row.result_digest, None),
        "approval_id": row.approval_id,
        "trace_id": row.trace_id,
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
    }


def _run_view(task: Task, steps: list[RunStep]) -> dict[str, Any]:
    """运行详情出参：状态 + 检查点摘要 + 步骤时间线。"""
    checkpoint = parse_checkpoint(task.checkpoint)
    try:
        next_step = int(checkpoint.get("next_step") or 0)
    except (TypeError, ValueError):  # 脏 checkpoint 不拖垮读取（与 runner 口径一致）
        next_step = 0
    return {
        "run_id": task.id,
        "agent": str(checkpoint.get("agent") or ""),
        "goal": str(checkpoint.get("goal") or ""),
        "username": task.username,
        "status": task.status,
        "status_label": state_label(task.status),
        "progress": float(task.progress or 0),
        "error": str(checkpoint.get("error") or ""),
        "next_step": next_step,
        "created_at": task.created_at.isoformat(sep=" ", timespec="seconds"),
        "steps": [_step_view(row) for row in steps],
    }


async def _get_run_or_raise(db: AsyncSession, tenant: str, run_id: str) -> Task:
    """按租户取 agent.run 类型的 Task 行；取不到 1004（越权与不存在同口径）。"""
    row = (
        await db.execute(
            select(Task).where(Task.tenant == tenant, Task.id == run_id, Task.type == RUN_TASK_TYPE)
        )
    ).scalar_one_or_none()
    if row is None:
        raise BusinessError(ErrorCode.NOT_FOUND, "运行记录不存在或无权访问", 404)
    return row


async def _run_steps(db: AsyncSession, tenant: str, run_id: str) -> list[RunStep]:
    """按 (step_index, created_at) 取时间线（失败重试的同步号多行按时间排列）。"""
    rows = await db.execute(
        select(RunStep)
        .where(RunStep.tenant == tenant, RunStep.run_id == run_id)
        .order_by(RunStep.step_index, RunStep.created_at)
    )
    return list(rows.scalars().all())


@router.get("/agents")
async def list_agents(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """可用智能体清单（每次实扫 plugins/*/agent.yaml，配置热更新零重启）。"""
    _ = user
    specs = loader.load_agent_specs()
    items = [spec.to_dict() for spec in specs]
    msg = "获取成功" if items else "暂无智能体配置，请在 plugins/*/agent.yaml 中声明"
    return ok({"total": len(items), "items": items}, msg)


@router.post("/runs")
async def create_run(
    payload: CreateRunRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """发起运行：受理即跑，code 0 返回 run 概要（执行失败在时间线里，前端轮询）。"""
    spec = loader.find_agent_spec(payload.agent.strip())
    summary = await start_run(
        db,
        spec=spec,
        goal=payload.goal.strip(),
        user=user,
        trace_id=current_trace_id(),
    )
    await db.commit()
    msg = (
        "运行完成"
        if summary["status"] == AgentState.DONE.value
        else "运行已受理，执行未完成，请查看时间线"
    )
    return ok(summary, msg)


@router.get("/runs/{run_id}")
async def run_detail(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """运行详情：状态 + 步骤时间线（每步含 trace_id / 审批单号，R2 起展示审批状态）。"""
    task = await _get_run_or_raise(db, user.tenant, run_id)
    steps = await _run_steps(db, user.tenant, run_id)
    return ok(_run_view(task, steps), "获取成功")


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """断点续跑：从 checkpoint 的 next_step 继续（agent/goal 也从检查点恢复）。

    DONE 是唯一终态（内核口径），已完成的运行拒绝续跑；R0 产生的失败均为 FAILED，
    可重新规划。WAITING_APPROVAL 的续跑口径（批准后 lazy resume）属 R2。
    """
    task = await _get_run_or_raise(db, user.tenant, run_id)
    if task.status == AgentState.DONE.value:
        raise BusinessError(ErrorCode.AGENT_STATE_ILLEGAL, "该运行已完成，无需续跑")
    checkpoint = parse_checkpoint(task.checkpoint)
    agent_name = str(checkpoint.get("agent") or "")
    goal = str(checkpoint.get("goal") or "")
    if not agent_name or not goal:
        raise BusinessError(ErrorCode.PARAM_INVALID, "检查点缺少智能体或目标信息，无法续跑")
    summary = await execute_run(
        db,
        task=task,
        spec=loader.find_agent_spec(agent_name),
        goal=goal,
        user=user,
        trace_id=current_trace_id(),
    )
    await db.commit()
    return ok(summary, "已从断点续跑")
