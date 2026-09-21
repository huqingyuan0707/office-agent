"""审批闭环（领域无关）：create / list / decide 最小三步。

职责：
- create()：executor 遇 needs_approval=True 工具时建单（pending），只建单不执行；
- list_all()：审批单列表（最新在前）；
- decide()：审批动作。批准 → 取回原 args 执行工具（复用 executor.execute_tool 超时底座）并落 tasks 结果；
  驳回 → 置 rejected。红线：审批人不得与提交人相同（同人 → ToolError 1001，中文报错）；全分支写 audit_logs。
链路：POST /tools/invoke（需审批工具）→ create；POST /approvals/{id}/decide → decide → execute_tool + tasks + audit_logs。
对齐：docs/office-agent仓库骨架与内核提取方案.md §3（policy.py 恒送审口径）、§5 M2（审批闸门验收）。
"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent import executor, registry
from office_agent.contracts import ToolError
from office_agent.db import Approval, record_audit, record_task, utcnow

ERR_SAME_PERSON = 1001


async def create(
    session: AsyncSession, *, tool_name: str, args: dict, requested_by: str, reason: str = ""
) -> Approval:
    """建审批单（pending）：只建单，不执行。"""
    approval = Approval(
        tool_name=tool_name,
        args_json=json.dumps(args, ensure_ascii=False),
        reason=reason,
        status="pending",
        requested_by=requested_by,
        created_at=utcnow(),
    )
    session.add(approval)
    await session.commit()
    await record_audit(
        session,
        actor=requested_by,
        action="approval.create",
        detail={"approval_id": approval.id, "tool_name": tool_name, "args": args, "reason": reason},
    )
    return approval


async def list_all(session: AsyncSession, limit: int = 100) -> list[Approval]:
    """审批单列表（最新在前）。"""
    rows = await session.execute(select(Approval).order_by(Approval.id.desc()).limit(limit))
    return list(rows.scalars())


async def decide(
    session: AsyncSession, *, approval_id: int, approver: str, approve: bool, comment: str = ""
) -> dict:
    """审批决定：approve=True 执行原工具并落 tasks 结果；approve=False 置 rejected。

    红线：审批人 ≠ 提交人（同人 → ToolError 1001）；已处理单不能重复审批（409）。
    """
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise ToolError(404, f"审批单不存在：#{approval_id}")
    if approval.status != "pending":
        raise ToolError(
            409, f"审批单 #{approval_id} 已处理（当前状态 {approval.status}），不能重复审批"
        )
    if approver == approval.requested_by:
        raise ToolError(
            ERR_SAME_PERSON, "审批人不能与提交人相同：请由其他用户完成审批（防自审自批红线）"
        )

    approval.decided_by = approver
    approval.comment = comment
    approval.decided_at = utcnow()

    if not approve:
        approval.status = "rejected"
        await session.commit()
        await record_audit(
            session,
            actor=approver,
            action="approval.decide",
            detail={
                "approval_id": approval_id,
                "tool_name": approval.tool_name,
                "outcome": "rejected",
                "comment": comment,
            },
        )
        return {"approval_id": approval_id, "status": "rejected", "comment": comment}

    spec = registry.get(approval.tool_name)  # 工具已被移除 → 404，单据保持 pending
    try:
        args = json.loads(approval.args_json or "{}")
    except json.JSONDecodeError:
        args = {}
    approval.status = "approved"
    await session.commit()
    trace_id = executor.new_trace_id()
    try:
        result = await executor.execute_tool(spec, args, trace_id)
    except ToolError as exc:
        await record_task(
            session,
            type_=approval.tool_name,
            status="failed",
            created_by=approval.requested_by,
            error=f"审批通过后执行失败：{exc.message}",
        )
        await record_audit(
            session,
            actor=approver,
            action="approval.decide",
            detail={
                "approval_id": approval_id,
                "outcome": "approved_execute_failed",
                "code": exc.code,
                "message": exc.message,
            },
            trace_id=trace_id,
        )
        raise
    task = await record_task(
        session,
        type_=approval.tool_name,
        status="succeeded",
        created_by=approval.requested_by,
        result=result,
    )
    await record_audit(
        session,
        actor=approver,
        action="approval.decide",
        detail={"approval_id": approval_id, "outcome": "approved_executed", "task_id": task.id},
        trace_id=trace_id,
    )
    return {
        "approval_id": approval_id,
        "status": "approved",
        "task_id": task.id,
        "trace_id": trace_id,
        "result": result,
    }
