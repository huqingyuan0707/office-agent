"""任务控制台端点（列表/统计/详情/操作/导出/登记，薄封装 → services/tasks.py）

链路：GET /tasks            → 列表（过滤/搜索/分页，数组契约：空库返回 [] 前端空态自己渲染）
      GET /tasks/stats      → 统计卡口径（总览/分状态/今日/本周/本月）
      GET /tasks/{id}       → 详情（input/output/error/checkpoint 解析 + 结果入口）
      POST /tasks           → 登记后台任务（对话/文档中心/拆解/定时选装的入台口子）
      POST /tasks/{id}/retry / cancel /  DELETE /tasks/{id} → 操作（写 tool_calls 审计）
      POST /tasks/{id}/export → 结果导出（md/csv/xlsx → base64 文本交付）

口径：本人 = tenant+username（默认 mine）；scope=all 且 admin 才可看/干预全租户；
      route 顺序：/tasks/stats 先于 /tasks/{id} 注册，避免 "stats" 被 task_id 吞掉；
      历史 agent.run 旧数据无扩展列，name/source/ref_* 由服务层按 checkpoint 推断。
对齐：AGENTS.md §3（分层红线/审计留痕/降级绝不 500）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core.errors import BusinessError
from office_agent_server.db import get_db
from office_agent_server.rbac import CurrentUser, get_current_user
from office_agent_server.responses import fail, ok
from office_agent_server.services import tasks as task_service

router = APIRouter(prefix="/tasks", tags=["tasks"])

#: 简写 filter 类型，避免端点签名过长
FilterStr = Annotated[str, Query(max_length=64)]
IntFilter = Annotated[int, Query(ge=1)]


def _is_admin(user: CurrentUser) -> bool:
    """admin 判定：角色含 admin 或 *（与 require_any_perm 同源口径）。"""
    return "admin" in user.roles or "*" in user.roles


def _audit_identity(db: AsyncSession, user: CurrentUser) -> str:
    """调用方事务的 trace_id（无则用稳定占位，保证审计行 name/args 可查）。"""
    from office_agent_server.middleware import current_trace_id

    return current_trace_id() or f"console:{user.username}"


@router.get("")
async def list_tasks(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
    status: str = Query(default="", max_length=16),
    task_type: str = Query(default="", alias="type", max_length=48),
    source: str = Query(default="", max_length=24),
    ref_kind: str = Query(default="", max_length=24),
    q: str = Query(default="", max_length=64),
    days: int = Query(default=0, ge=0, le=365),
    scope: str = Query(default="mine", max_length=8),
) -> dict:
    """任务列表（数组契约保持：空库返回 []；scope=all 仅 admin 可见全租户）。"""
    data = await task_service.list_tasks(
        db,
        tenant=user.tenant,
        username=user.username,
        is_admin=_is_admin(user),
        status=status,
        task_type=task_type,
        source=source,
        ref_kind=ref_kind,
        q=q,
        days=days,
        scope=scope,
        page=page,
        size=size,
    )
    return ok(data["rows"], "获取成功")


@router.get("/stats")
async def task_stats(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
    scope: str = Query(default="mine", max_length=8),
) -> dict:
    """统计卡口径（scope=all 仅 admin 可用）。"""
    data = await task_service.task_stats(
        db,
        tenant=user.tenant,
        username=user.username,
        is_admin=_is_admin(user),
        scope=scope,
    )
    return ok(data, "获取成功")


@router.get("/{task_id}")
async def task_detail(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """单任务详情（越权/不存在同口径 1004）。"""
    try:
        return ok(
            await task_service.get_task(
                db,
                tenant=user.tenant,
                username=user.username,
                is_admin=_is_admin(user),
                task_id=task_id,
            ),
            "获取成功",
        )
    except BusinessError as exc:
        return fail(exc.code, exc.msg, http_status=exc.http_status)


@router.post("")
async def create_task(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
    name: str = Query(..., min_length=1, max_length=200),
    task_type: str = Query(..., alias="type", max_length=48),
    source: str = Query("manual", max_length=24),
    ref_kind: str = Query("", max_length=24),
    ref_id: str = Query("", max_length=64),
    ref_label: str = Query("", max_length=200),
    status: str = Query("pending", max_length=16),
    progress: float = Query(0, ge=0, le=100),
    note: str = Query("", max_length=500),
) -> dict:
    """登记后台任务（返回序列化视图，写 tool_calls 审计留痕）。"""
    try:
        return ok(
            await task_service.create_task(
                db,
                tenant=user.tenant,
                username=user.username,
                trace_id=_audit_identity(db, user),
                name=name,
                task_type=task_type,
                source=source,
                ref_kind=ref_kind,
                ref_id=ref_id,
                ref_label=ref_label,
                status=status,
                progress=progress,
                note=note,
            ),
            "任务已登记",
        )
    except BusinessError as exc:
        return fail(exc.code, exc.msg)


@router.post("/{task_id}/retry")
async def retry_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """重试任务（本人 或 admin；终态外拒绝，写审计）。"""
    return await _run_action(task_service.retry_task, db, user, task_id, "任务已重新排队")


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """取消任务（仅排队/执行中/待确认，写审计）。"""
    return await _run_action(task_service.cancel_task, db, user, task_id, "任务已取消")


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除任务记录（写审计，审计不删）。"""
    return await _run_action(task_service.delete_task, db, user, task_id, "任务记录已删除")


@router.post("/{task_id}/export")
async def export_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
    fmt: str = Query("md", max_length=8),
) -> dict:
    """任务结果导出（md/csv/xlsx → base64 文本；写审计）。"""
    try:
        return ok(
            await task_service.export_task(
                db,
                tenant=user.tenant,
                username=user.username,
                is_admin=_is_admin(user),
                trace_id=_audit_identity(db, user),
                task_id=task_id,
                fmt=fmt,
            ),
            "导出成功",
        )
    except BusinessError as exc:
        return fail(exc.code, exc.msg, http_status=exc.http_status)


async def _run_action(action, db: AsyncSession, user: CurrentUser, task_id: str, msg: str) -> dict:
    """操作动作统一包装：调服务 → ok/fail 信封（业务错误带可操作 msg）。"""
    try:
        return ok(
            await action(
                db,
                tenant=user.tenant,
                username=user.username,
                is_admin=_is_admin(user),
                trace_id=_audit_identity(db, user),
                task_id=task_id,
            ),
            msg,
        )
    except BusinessError as exc:
        return fail(exc.code, exc.msg, http_status=exc.http_status)
