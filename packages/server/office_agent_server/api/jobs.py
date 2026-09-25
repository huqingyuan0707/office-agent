"""定时任务端点（作业 CRUD + 立即执行）

链路：端点薄封装（解析 → services/jobs → ok()）→ scheduled_jobs 表 + 执行链。
口径：作业定义按租户隔离（跨租户 404）；执行身份 = 创建人实时角色，
      审批闸门在 workflow_run 执行链里同样绕不过；立即执行不推进定时节奏。
分层红线：schedule 校验/到点换算/执行分发全在 service 层，端点只做 DTO 与 ok()。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_server.db import get_db
from office_agent_server.rbac import CurrentUser, get_current_user, require_any_perm
from office_agent_server.responses import ok
from office_agent_server.services import jobs as job_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobCreateBody(BaseModel):
    """新建定时任务 DTO（schedule/payload 为自由 JSON，合法性由 service 校验）。"""

    name: str = Field(min_length=1, max_length=80)
    job_type: str = Field(min_length=1, max_length=32)
    schedule: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class JobUpdateBody(BaseModel):
    """更新定时任务 DTO（四者全空由 service 层 1001；此处全可选）。"""

    name: str | None = Field(default=None, max_length=80)
    schedule: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    enabled: bool | None = None


@router.get("")
async def list_jobs(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """作业列表（租户内共享定义；登录即可看本租户的定时任务）。"""
    return ok(await job_service.list_jobs(db, tenant=user.tenant), "获取成功")


@router.post("")
async def create_job(
    payload: JobCreateBody,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """新建作业（类型/schedule 非法 1001，workflow_run 绑定的工作流不存在 1004）。"""
    return ok(
        await job_service.create_job(
            db,
            tenant=user.tenant,
            name=payload.name,
            job_type=payload.job_type,
            schedule=payload.schedule,
            payload=payload.payload,
            created_by=user.username,
        ),
        "创建成功",
    )


@router.put("/{job_id}")
async def update_job(
    job_id: str,
    payload: JobUpdateBody,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """更新作业（改 schedule 或重新启用都从当下重算下次执行，停用期间欠账不补跑）。"""
    return ok(
        await job_service.update_job(
            db,
            tenant=user.tenant,
            job_id=job_id,
            name=payload.name,
            schedule=payload.schedule,
            payload=payload.payload,
            enabled=payload.enabled,
        ),
        "更新成功",
    )


@router.delete("/{job_id}")
async def delete_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """删除作业（只删定义，历史审计留痕不断）。"""
    return ok(await job_service.delete_job(db, tenant=user.tenant, job_id=job_id), "删除成功")


@router.post("/{job_id}/run")
async def run_job_now(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """立即执行一次（手动触发不改 next_run_at；执行结果如实返回含 failed 分支）。"""
    return ok(await job_service.run_job_now(db, tenant=user.tenant, job_id=job_id), "执行完成")


@router.post("/tick")
async def tick_jobs(
    db: AsyncSession = Depends(get_db),
    _: CurrentUser = Depends(require_any_perm("admin")),
) -> dict[str, Any]:
    """手动扫描到点作业（调度环的等价入口：未开 SCHEDULER_ENABLED 时可由外部 cron 调用）。"""
    return ok(await job_service.tick_due_jobs(db), "扫描完成")
