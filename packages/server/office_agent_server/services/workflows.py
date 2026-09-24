"""工作流编排服务（多场景串联的业务逻辑，纯函数不含 FastAPI 对象）。

职责：工作流 CRUD（租户隔离，步骤落库前校验）+ run_workflow() 顺序执行——
      逐步调内核 executor.call（Scope/熔断/审计全在内核，绕不过）；需审批步骤
      只落单即停（pending_approval，后续步骤不跑，绝不越过审批闸门）；中途失败
      即停并如实返回已完成步骤（已生效步骤不谎称回滚）。

链路：api/workflows（解析 → 本模块 → ok()）→ workflows 表 + registry/executor。
RPA 口径：远程工具步骤与本地步骤同一条执行链，出站溯源（provider_id +
      provenance）随步骤结果返回——外部系统动作被编排、被审计，即本仓库的
      RPA 联动形态，不另造一套 RPA 引擎。
对齐：AGENTS.md §3（分层/写动作恒送审/降级绝不 500）；智能办公Agent 产品需求文档.md
      §2.11（场景串联/可视化工作流编排）、§5.3（RPA 联动）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core import executor, registry
from office_agent_core.contracts import ToolContext, validate_args
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_server.db import _now
from office_agent_server.models import Workflow
from office_agent_server.services.approval_flow import create_approval

logger = logging.getLogger(__name__)

#: 单工作流步骤上限（可视化编排一屏可读；超限请拆多个工作流串接）
MAX_STEPS = 20


def parse_steps(raw: Any) -> list[dict[str, Any]]:
    """步骤 JSON 口径：数组 + 每步 {tool: 非空串, args: 对象} + 上限（非法一律 1001）。"""
    steps = raw
    if isinstance(raw, str):
        try:
            steps = json.loads(raw or "[]")
        except ValueError as exc:
            raise BusinessError(ErrorCode.PARAM_INVALID, "工作流步骤不是合法 JSON") from exc
    if not isinstance(steps, list) or not steps:
        raise BusinessError(ErrorCode.PARAM_INVALID, "工作流至少包含 1 个步骤")
    if len(steps) > MAX_STEPS:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"工作流最多 {MAX_STEPS} 步（当前 {len(steps)}）"
        )
    parsed: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not str(step.get("tool") or "").strip():
            raise BusinessError(ErrorCode.PARAM_INVALID, f"第 {index + 1} 步缺少工具名")
        args = step.get("args") or {}
        if not isinstance(args, dict):
            raise BusinessError(ErrorCode.PARAM_INVALID, f"第 {index + 1} 步的 args 必须是键值对")
        parsed.append({"tool": str(step["tool"]).strip(), "args": args})
    return parsed


def validate_steps(steps: list[dict[str, Any]]) -> None:
    """执行前全量校验：工具存在（4005）+ 入参合法（1001），fail-fast 在任何步骤执行之前。"""
    for index, step in enumerate(steps):
        spec = registry.get(step["tool"])
        errors = validate_args(spec.params, step["args"])
        if errors:
            raise BusinessError(
                ErrorCode.PARAM_INVALID, f"第 {index + 1} 步参数有误：{'；'.join(errors)}"
            )


async def _get_or_raise(db: AsyncSession, tenant: str, workflow_id: str) -> Workflow:
    """按租户取工作流；取不到 404（越权与不存在同口径，不泄漏他租户编排）。"""
    row = (
        await db.execute(
            select(Workflow).where(Workflow.tenant == tenant, Workflow.id == workflow_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise BusinessError(ErrorCode.NOT_FOUND, "工作流不存在或无权访问", 404)
    return row


def to_dict(row: Workflow, *, with_steps: bool = False) -> dict[str, Any]:
    """工作流出参（列表默认不带 steps 全文，只给步数；详情才展开）。"""
    try:
        steps = parse_steps(row.steps)
    except BusinessError:
        steps = []
    view: dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "step_count": len(steps),
        "created_by": row.created_by,
        "updated_at": row.updated_at.isoformat(sep=" ", timespec="seconds"),
    }
    if with_steps:
        view["steps"] = steps
    return view


async def get_workflow(db: AsyncSession, *, tenant: str, workflow_id: str) -> dict[str, Any]:
    """工作流详情（含 steps 全文；跨租户 404）。"""
    return to_dict(await _get_or_raise(db, tenant, workflow_id), with_steps=True)


async def list_workflows(db: AsyncSession, *, tenant: str) -> list[dict[str, Any]]:
    """租户内工作流列表（按更新倒序；定义是租户共享编排，不做本人过滤）。"""
    rows = (
        (
            await db.execute(
                select(Workflow)
                .where(Workflow.tenant == tenant)
                .order_by(Workflow.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [to_dict(row) for row in rows]


async def create_workflow(
    db: AsyncSession,
    *,
    tenant: str,
    name: str,
    description: str,
    steps: Any,
    created_by: str,
) -> dict[str, Any]:
    """新建工作流（步骤先校验再落库，脏编排不入库）。"""
    text = str(name or "").strip()
    if not text:
        raise BusinessError(ErrorCode.PARAM_INVALID, "工作流名称不能为空")
    parsed = parse_steps(steps)
    validate_steps(parsed)
    row = Workflow(
        tenant=tenant,
        name=text[:80],
        description=str(description or "").strip()[:200],
        steps=json.dumps(parsed, ensure_ascii=False),
        created_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return to_dict(row, with_steps=True)


async def update_workflow(
    db: AsyncSession,
    *,
    tenant: str,
    workflow_id: str,
    name: str | None,
    description: str | None,
    steps: Any,
) -> dict[str, Any]:
    """更新工作流（steps 传了就全量替换并重校验；三者全空 1001）。"""
    row = await _get_or_raise(db, tenant, workflow_id)
    if name is None and description is None and steps is None:
        raise BusinessError(ErrorCode.PARAM_INVALID, "没有要更新的内容")
    if name is not None:
        text = str(name).strip()
        if not text:
            raise BusinessError(ErrorCode.PARAM_INVALID, "工作流名称不能为空")
        row.name = text[:80]
    if description is not None:
        row.description = str(description).strip()[:200]
    if steps is not None:
        parsed = parse_steps(steps)
        validate_steps(parsed)
        row.steps = json.dumps(parsed, ensure_ascii=False)
    row.updated_at = _now()
    await db.commit()
    await db.refresh(row)
    return to_dict(row, with_steps=True)


async def delete_workflow(db: AsyncSession, *, tenant: str, workflow_id: str) -> dict[str, str]:
    """删除工作流（只删定义；已产生的审批单/审计不受影响，留痕不断链）。"""
    row = await _get_or_raise(db, tenant, workflow_id)
    await db.delete(row)
    await db.commit()
    return {"deleted": workflow_id}


async def run_workflow(
    db: AsyncSession,
    *,
    tenant: str,
    username: str,
    roles: list[str],
    workflow_id: str,
    trace_id: str = "",
) -> dict[str, Any]:
    """顺序执行工作流（全量先校验 → 逐步 executor.call → 审批步落单即停 → 失败即停）。

    返回 {status, steps:[{index/tool/status/latency_ms/...}], ...}：status 为
    ok（全跑完）/ pending_approval（某步送审，后续没跑）/ failed（某步抛错）。
    每步 Scope 按执行人角色在内核里硬拦——越权步骤执行即停，不跳过。
    """
    row = await _get_or_raise(db, tenant, workflow_id)
    steps = parse_steps(row.steps)
    validate_steps(steps)
    ctx = ToolContext(db=db, tenant=tenant, username=username, roles=list(roles), trace_id=trace_id)
    done: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        spec = registry.get(step["tool"])
        if spec.requires_approval:
            approval = await create_approval(
                db,
                tenant=tenant,
                tool_name=spec.name,
                args=step["args"],
                applicant=username,
                reason=f"工作流「{row.name}」第 {index + 1} 步",
                target=row.name,
            )
            await db.commit()
            return {
                "workflow_id": row.id,
                "workflow_name": row.name,
                "status": "pending_approval",
                "approval_id": approval.id,
                "next_step": index,
                "steps": done,
                "trace_id": trace_id,
            }
        try:
            outcome = await executor.call(
                ctx, name=step["tool"], args=step["args"], trace_id=trace_id
            )
        except BusinessError as exc:
            await db.commit()
            logger.warning("workflow %s stopped at step %d: %s", row.id, index, exc.msg[:120])
            return {
                "workflow_id": row.id,
                "workflow_name": row.name,
                "status": "failed",
                "error": exc.msg,
                "code": exc.code,
                "failed_step": index,
                "steps": done,
                "trace_id": trace_id,
            }
        done.append(
            {
                "index": index,
                "tool": step["tool"],
                "status": "ok",
                "latency_ms": outcome.get("latency_ms"),
                "approval_id": outcome.get("approval_id") or "",
                "provider_id": outcome.get("provider_id") or "",
                "provenance": outcome.get("provenance"),
                "result": outcome.get("result"),
            }
        )
    await db.commit()
    return {
        "workflow_id": row.id,
        "workflow_name": row.name,
        "status": "ok",
        "steps": done,
        "trace_id": trace_id,
    }
