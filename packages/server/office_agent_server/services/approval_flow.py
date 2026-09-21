"""审批业务服务（纯函数，不含 FastAPI 对象）。

职责：
- create_approval()：需审批工具 invoke 时建审批单（pending），只落单不执行；
- decide_approval()：批准后以**提交人身份**执行原工具；驳回则置 rejected；
  红线：审批人 == 提交人直接抛 1001（同人自审自批口子）。

链路：api/tools.invoke → spec.requires_approval → create_approval() → 返回 pending_approval；
      api/approvals.approve → decide_approval() → 状态流转 + 同人红线 + executor.call 以提交人身份。
对齐：AGENTS.md §3（分层红线：业务进 services/ 纯函数，禁止裸 db 操作）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core import executor, registry
from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_server.db import _now
from office_agent_server.models import Approval

logger = logging.getLogger(__name__)

#: 同人自审自批红线号段（1xxx 通用段：参数/业务约束）
ERR_SAME_PERSON = 1001


async def create_approval(
    db: AsyncSession,
    *,
    tenant: str,
    tool_name: str,
    args: dict[str, Any],
    applicant: str,
    session_id: str = "",
    reason: str = "",
    target: str = "",
) -> Approval:
    """建审批单（pending）：只落单，不执行工具。

    端点先过 registry.get + validate_args，本函数假定参数已校验。
    """
    row = Approval(
        tenant=tenant,
        session_id=session_id,
        action=tool_name,
        target=target or tool_name,
        args=json.dumps(args, ensure_ascii=False),
        reason=reason,
        applicant=applicant,
        approver="",
        status="pending",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "approval created: id=%s tool=%s applicant=%s tenant=%s",
        row.id,
        tool_name,
        applicant,
        tenant,
    )
    return row


async def _get_pending(db: AsyncSession, tenant: str, approval_id: str) -> Approval:
    """取 pending 状态审批单；取不到（含越权/已处理）抛对应码。"""
    row = (
        await db.execute(
            select(Approval).where(Approval.tenant == tenant, Approval.id == approval_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise BusinessError(ErrorCode.NOT_FOUND, "审批单不存在或无权访问", 404)
    if row.status != "pending":
        label_map = {"approved": "已通过", "rejected": "已驳回"}
        label = label_map.get(row.status, row.status)
        raise BusinessError(
            ErrorCode.APPROVAL_DENIED,
            f"该审批已是「{label}」，不能重复处理",
        )
    return row


async def decide_approval(
    db: AsyncSession,
    *,
    tenant: str,
    approval_id: str,
    approver: str,
    approve: bool,
    reason: str,
    trace_id: str = "",
) -> dict[str, Any]:
    """审批决定业务逻辑（纯函数，端点只做参数解析 + ok()/fail()）。

    红线：
    - 同人自审自批 → 1001；
    - 批准后以**提交人身份**执行工具（owner 是申请人，approver 只做决策）；
    - 驳回理由必填；
    - 执行失败 → 审批单保持 approved 状态但标记执行异常（结果留痕便于排障）。

    返回：审批结果信封（含 execution_result 执行回执）。
    """
    row = await _get_pending(db, tenant, approval_id)

    # --- 同人红线 ---
    if approver == row.applicant:
        raise BusinessError(
            ERR_SAME_PERSON,
            "审批人不能与提交人相同：请由其他用户完成审批（防自审自批红线）",
        )

    # --- 驳回分支 ---
    text = reason.strip()
    if not approve:
        if not text:
            raise BusinessError(ErrorCode.PARAM_INVALID, "驳回理由必填，请填写后再驳回")
        row.status = "rejected"
        row.approver = approver
        row.reason = f"{row.reason}｜驳回原因：{text}"[:200]
        row.decided_at = _now()
        await db.commit()
        logger.info("approval rejected: id=%s approver=%s", row.id, approver)
        return {
            "approval_id": row.id,
            "status": "rejected",
            "approver": approver,
            "reason": row.reason,
            "decided_at": row.decided_at.isoformat(sep=" ", timespec="seconds")
            if row.decided_at
            else "",
        }

    # --- 批准分支：先置 approved，再以提交人身份执行 ---
    row.status = "approved"
    row.approver = approver
    if text:
        row.reason = f"{row.reason}｜批准说明：{text}"[:200]
    row.decided_at = _now()
    await db.commit()

    # 解析 args JSON；脏数据给空对象（不因历史脏数据把批准流打断）
    try:
        tool_args = json.loads(row.args or "{}")
    except json.JSONDecodeError:
        tool_args = {}
    if not isinstance(tool_args, dict):
        tool_args = {}

    # 工具已被移除 → 执行失败留痕但审批单状态不回滚
    execution_result: dict[str, Any]
    try:
        registry.get(row.action)
    except BusinessError as exc:
        execution_result = {"status": "failed", "message": exc.msg, "code": exc.code}
        logger.warning("approved tool missing after approval: id=%s tool=%s", row.id, row.action)
    else:
        ctx = ToolContext(
            db=db,
            tenant=row.tenant,
            username=row.applicant,  # 关键：以**提交人**身份执行
            roles=[],  # 执行时 scope 已在 invoke 时鉴权过，此处传空即可
            trace_id=trace_id,
        )
        try:
            execution_result = await executor.call(
                ctx, name=row.action, args=tool_args, trace_id=trace_id
            )
        except BusinessError as exc:
            execution_result = {"status": "failed", "message": exc.msg, "code": exc.code}
            logger.exception(
                "approved tool execute failed: id=%s tool=%s err=%s", row.id, row.action, exc.msg
            )
        except Exception as exc:
            execution_result = {"status": "failed", "message": str(exc)[:200]}
            logger.exception("approved tool unexpected error: id=%s tool=%s", row.id, row.action)

    return {
        "approval_id": row.id,
        "status": "approved",
        "approver": approver,
        "applicant": row.applicant,
        "tool": row.action,
        "execution_result": execution_result,
        "decided_at": row.decided_at.isoformat(sep=" ", timespec="seconds")
        if row.decided_at
        else "",
        "trace_id": trace_id,
    }
