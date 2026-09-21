"""审批端点（列表 / 详情 / 批驳）

链路：审批中心 → 本模块 → approvals 表（按 tenant 隔离）。
口径：审批是写动作的唯一放行口——内核策略恒送审，生成审批单由需审批的工具实现负责；
      本模块只做读口径与状态流转，**不伪造业务生效结果**（写层工具落地后由其在批/驳回调里消费）。

驳回必须填理由：没有理由的驳回在回放时等于没有信息。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_server.db import _now, get_db
from office_agent_server.models import Approval
from office_agent_server.rbac import CurrentUser, require_any_perm
from office_agent_server.responses import ok

router = APIRouter(prefix="/approvals", tags=["approvals"])

STATUS_LABELS: dict[str, str] = {
    "pending": "待审批",
    "approved": "已通过",
    "rejected": "已驳回",
}

#: 审批列表可见：登录即可看本租户待办（内核不预设具体业务域角色名）
LIST_PERMS = ("admin", "viewer")
#: 批/驳只能由有权决策的角色处理（通配角色 ``*`` 由 require_any_perm 统一放行）
APPROVER_PERMS = ("admin", "approver")


class ApproveRequest(BaseModel):
    """批准入参（可附批准说明）。"""

    reason: str = Field(default="", max_length=200)


class RejectRequest(BaseModel):
    """驳回入参（理由必填）。"""

    reason: str = Field(default="", max_length=200)


def _parse_args(row: Approval) -> dict[str, Any]:
    """args 列存 JSON 文本；解析失败给空对象（不因脏数据把列表整体打挂）。"""
    try:
        parsed = json.loads(row.args or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_dict(row: Approval) -> dict[str, Any]:
    """审批单出参（前端审批中心直接渲染）。"""
    return {
        "id": row.id,
        "action": row.action,
        "target": row.target,
        "args": _parse_args(row),
        "reason": row.reason,
        "applicant": row.applicant,
        "approver": row.approver,
        "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "session_id": row.session_id,
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
        "decided_at": (
            row.decided_at.isoformat(sep=" ", timespec="seconds") if row.decided_at else ""
        ),
    }


async def _get_or_raise(db: AsyncSession, tenant: str, approval_id: str) -> Approval:
    """按租户取审批单，取不到 1004（越权与不存在同口径，不泄漏他租户数据）。"""
    row = (
        await db.execute(
            select(Approval).where(Approval.tenant == tenant, Approval.id == approval_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise BusinessError(ErrorCode.NOT_FOUND, "审批单不存在或无权访问", 404)
    return row


def _decide(row: Approval, *, approve: bool, approver: str, reason: str) -> None:
    """状态流转（已处理过的不允许重复处理；理由追加留痕，与既有回放口径一致）。"""
    if row.status != "pending":
        label = STATUS_LABELS.get(row.status, row.status)
        raise BusinessError(ErrorCode.APPROVAL_DENIED, f"该审批已是「{label}」，不能重复处理")
    text = reason.strip()
    if not approve and not text:
        raise BusinessError(ErrorCode.PARAM_INVALID, "驳回理由必填，请填写后再驳回")
    if approve:
        row.status = "approved"
        if text:
            row.reason = f"{row.reason}｜批准说明：{text}"[:200]
    else:
        row.status = "rejected"
        row.reason = f"{row.reason}｜驳回原因：{text}"[:200]
    row.approver = approver
    row.decided_at = _now()


@router.get("")
async def list_approvals(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_any_perm(*LIST_PERMS)),
    status: str = Query(default="pending", max_length=16),
    action: str = Query(default="", max_length=48),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """审批列表（默认只看待办，按创建时间倒序）。"""
    stmt = select(Approval).where(Approval.tenant == user.tenant)
    if status:
        stmt = stmt.where(Approval.status == status)
    if action:
        stmt = stmt.where(Approval.action == action)
    stmt = stmt.order_by(Approval.created_at.desc()).offset((page - 1) * size).limit(size)
    rows = (await db.execute(stmt)).scalars().all()
    return ok([_to_dict(row) for row in rows], "获取成功")


@router.get("/{approval_id}")
async def approval_detail(
    approval_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_any_perm(*LIST_PERMS)),
) -> dict[str, Any]:
    """审批详情（跨租户 404）。"""
    row = await _get_or_raise(db, user.tenant, approval_id)
    return ok(_to_dict(row), "获取成功")


@router.post("/{approval_id}/approve")
async def approve(
    approval_id: str,
    payload: ApproveRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_any_perm(*APPROVER_PERMS)),
) -> dict[str, Any]:
    """批准（状态流转留痕，谁在何时批的可在审批表与审计里回溯）。"""
    row = await _get_or_raise(db, user.tenant, approval_id)
    _decide(row, approve=True, approver=user.username, reason=payload.reason)
    await db.commit()
    return ok(_to_dict(row), "审批已通过")


@router.post("/{approval_id}/reject")
async def reject(
    approval_id: str,
    payload: RejectRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_any_perm(*APPROVER_PERMS)),
) -> dict[str, Any]:
    """驳回：被申请的动作不执行，理由追加留痕。"""
    row = await _get_or_raise(db, user.tenant, approval_id)
    _decide(row, approve=False, approver=user.username, reason=payload.reason)
    await db.commit()
    return ok(_to_dict(row), "已驳回，原数据保持不变")