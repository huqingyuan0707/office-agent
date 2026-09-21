"""任务端点（本人维度列表）

链路：GET /tasks → 按 (tenant, username) 过滤 → 倒序分页 → ok(数组)。
真实空数据返回 ``[]`` 不报错（前端空态自己渲染）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_server.db import get_db
from office_agent_server.models import Task
from office_agent_server.rbac import CurrentUser, get_current_user
from office_agent_server.responses import ok

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _task_to_dict(row: Task) -> dict[str, Any]:
    """任务出参（时间统一 ``YYYY-MM-DD HH:MM:SS``，与既有落库口径一致）。"""
    return {
        "id": row.id,
        "type": row.type,
        "status": row.status,
        "progress": row.progress,
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
    }


@router.get("")
async def list_tasks(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    status: str = Query(default="", max_length=16),
) -> dict[str, Any]:
    """任务列表（本人维度；跨用户/跨租户一律不可见）。"""
    stmt = select(Task).where(Task.tenant == user.tenant, Task.username == user.username)
    if status:
        stmt = stmt.where(Task.status == status)
    stmt = stmt.order_by(Task.created_at.desc()).offset((page - 1) * size).limit(size)
    rows = (await db.execute(stmt)).scalars().all()
    return ok([_task_to_dict(row) for row in rows], "获取成功")