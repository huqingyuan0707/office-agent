"""工作流编排端点（定义 CRUD + 顺序执行）

链路：端点薄封装（解析 → services/workflows → ok()）→ workflows 表 + 内核执行链。
口径：定义是租户共享编排（列表不做本人过滤）；执行按执行人角色逐 Scope 硬拦；
      写步骤遇审批只落单即停——审批闸门在执行链里同样绕不过。
分层红线：步骤校验/执行/落单全在 service 层，端点只做 DTO 解析与 ok()。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_server.db import get_db
from office_agent_server.middleware import current_trace_id
from office_agent_server.rbac import CurrentUser, get_current_user
from office_agent_server.responses import ok
from office_agent_server.services import workflows as workflow_service

router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowStepBody(BaseModel):
    """单步骤 DTO（tool 必填；args 缺省空对象）。"""

    tool: str = Field(min_length=1, max_length=120)
    args: dict[str, Any] = Field(default_factory=dict)


class WorkflowCreateBody(BaseModel):
    """新建工作流入参（端点私有 DTO）。"""

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=200)
    steps: list[WorkflowStepBody] = Field(min_length=1, max_length=20)


class WorkflowUpdateBody(BaseModel):
    """更新工作流入参（三者全空由 service 层 1001；此处全可选）。"""

    name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=200)
    steps: list[WorkflowStepBody] | None = Field(default=None, max_length=20)


@router.get("")
async def list_workflows(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """工作流列表（租户共享定义；登录即可看本租户的编排）。"""
    return ok(await workflow_service.list_workflows(db, tenant=user.tenant), "获取成功")


@router.post("")
async def create_workflow(
    payload: WorkflowCreateBody,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """新建工作流（步骤非法直接 1001，脏编排不入库）。"""
    return ok(
        await workflow_service.create_workflow(
            db,
            tenant=user.tenant,
            name=payload.name,
            description=payload.description,
            steps=[step.model_dump() for step in payload.steps],
            created_by=user.username,
        ),
        "创建成功",
    )


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """工作流详情（含 steps 全文，跨租户 404）。"""
    return ok(
        await workflow_service.get_workflow(db, tenant=user.tenant, workflow_id=workflow_id),
        "获取成功",
    )


@router.put("/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    payload: WorkflowUpdateBody,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """更新工作流（steps 全量替换并重校验）。"""
    return ok(
        await workflow_service.update_workflow(
            db,
            tenant=user.tenant,
            workflow_id=workflow_id,
            name=payload.name,
            description=payload.description,
            steps=(
                [step.model_dump() for step in payload.steps] if payload.steps is not None else None
            ),
        ),
        "更新成功",
    )


@router.delete("/{workflow_id}")
async def delete_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """删除工作流（只删定义，已产审批/审计留痕不断）。"""
    return ok(
        await workflow_service.delete_workflow(db, tenant=user.tenant, workflow_id=workflow_id),
        "删除成功",
    )


@router.post("/{workflow_id}/run")
async def run_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """执行工作流（顺序跑：需审批步骤落单即停 pending_approval，失败即停 failed）。"""
    return ok(
        await workflow_service.run_workflow(
            db,
            tenant=user.tenant,
            username=user.username,
            roles=list(user.roles),
            workflow_id=workflow_id,
            trace_id=current_trace_id(),
        ),
        "执行完成",
    )
