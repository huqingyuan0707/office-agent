"""审批端点（列表 / 详情 / 批驳）

链路：审批中心 → 本模块 → services/approval_flow（业务逻辑） → approvals 表。
口径：审批是写动作的唯一放行口——内核策略恒送审，生成审批单由需审批工具的 invoke 负责；
      本模块只做读口径 + 状态流转 + 调 service，**不在端点内直接裸写业务逻辑**。
驳回必须填理由：没有理由的驳回在回放时等于没有信息。

分层红线：approve/reject 端点薄封装——解析入参 → 调 services.approval_flow.decide_approval → ok()；
          同人红线（审批人 == 提交人 → 1001）在 service 层统一拦，端点不复制。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_server.db import get_db
from office_agent_server.middleware import current_trace_id
from office_agent_server.models import Approval
from office_agent_server.rbac import CurrentUser, require_any_perm
from office_agent_server.responses import ok
from office_agent_server.services.approval_flow import decide_approval

router = APIRouter(prefix="/approvals", tags=["approvals"])

STATUS_LABELS: dict[str, str] = {
    "pending": "待审批",
    "approved": "已通过",
    "rejected": "已驳回",
}

#: 审批列表可见：登录即可看本租户待办
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
    """批准（状态流转 + 同人红线 + 以提交人身份执行工具——全在 service 层）。"""
    trace = current_trace_id()
    result = await decide_approval(
        db,
        tenant=user.tenant,
        approval_id=approval_id,
        approver=user.username,
        approve=True,
        reason=payload.reason,
        trace_id=trace,
    )
    return ok(result, "审批已通过")


@router.post("/{approval_id}/reject")
async def reject(
    approval_id: str,
    payload: RejectRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_any_perm(*APPROVER_PERMS)),
) -> dict[str, Any]:
    """驳回：被申请的动作不执行，理由追加留痕（同人红线 service 层统一拦）。"""
    trace = current_trace_id()
    result = await decide_approval(
        db,
        tenant=user.tenant,
        approval_id=approval_id,
        approver=user.username,
        approve=False,
        reason=payload.reason,
        trace_id=trace,
    )
    return ok(result, "已驳回，原数据保持不变")
