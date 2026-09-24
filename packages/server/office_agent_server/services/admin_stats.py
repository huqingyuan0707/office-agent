"""管理员运营聚合（平台使用统计看板的唯一口径源，纯函数不含 FastAPI 对象）。

职责：collect_overview() 按租户聚合——用户分状态计数 / 任务分状态计数 /
      审批分状态计数 / 工具调用总量 + Top 工具 / 最近裁决 5 单。
链路：api/admin.GET /admin/overview（admin 二次鉴权）→ 本模块 → users/tasks/
      approvals/tool_calls 四表只读聚合。
口径：只计数、不回行明细（名单类数据不出聚合口）；Top 工具按调用次数倒序取 8；
      最近裁决只看已决单（pending 不在“裁决”里），decided_at 为空的脏行排最后。
对齐：AGENTS.md §3（分层红线：业务进 services/纯函数）；智能办公Agent 产品需求文档.md
      §2.13（平台使用统计看板：Agent 使用率/高频指令/功能访问统计的计数底座）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_server.models import Approval, Task, ToolCall, User

#: Top 工具榜单长度（看板一屏可读，不做分页）
_TOP_TOOLS_LIMIT = 8
#: 最近裁决条数（只给“刚刚发生了什么”，翻页去审批页）
_RECENT_DECISIONS_LIMIT = 5


async def _count_by_status(db: AsyncSession, model: Any, tenant: str) -> dict[str, int]:
    """按 status 分组计数（Task/Approval 通用；User 另走 _count_users）。"""
    rows = (
        await db.execute(
            select(model.status, func.count()).where(model.tenant == tenant).group_by(model.status)
        )
    ).all()
    return {str(status): int(count) for status, count in rows}


async def collect_overview(db: AsyncSession, *, tenant: str) -> dict[str, Any]:
    """租户运营总览（四表只读聚合，无写副作用，可安全地被看板高频轮询）。"""
    user_rows = (
        await db.execute(
            select(User.status, func.count()).where(User.tenant == tenant).group_by(User.status)
        )
    ).all()
    users = {str(status): int(count) for status, count in user_rows}
    tasks = await _count_by_status(db, Task, tenant)
    approvals = await _count_by_status(db, Approval, tenant)
    tool_total = (
        await db.execute(
            select(func.count()).select_from(ToolCall).where(ToolCall.tenant == tenant)
        )
    ).scalar_one()
    top_rows = (
        await db.execute(
            select(ToolCall.name, func.count().label("cnt"))
            .where(ToolCall.tenant == tenant)
            .group_by(ToolCall.name)
            .order_by(desc("cnt"))
            .limit(_TOP_TOOLS_LIMIT)
        )
    ).all()
    decided_rows = (
        (
            await db.execute(
                select(Approval)
                .where(Approval.tenant == tenant, Approval.status != "pending")
                .order_by(Approval.decided_at.desc())
                .limit(_RECENT_DECISIONS_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return {
        "users": {
            "total": sum(users.values()),
            "active": users.get("active", 0),
            "frozen": users.get("frozen", 0),
        },
        "tasks": tasks,
        "approvals": approvals,
        "tool_calls": {
            "total": int(tool_total),
            "top_tools": [{"name": name, "count": int(count)} for name, count in top_rows],
        },
        "recent_decisions": [
            {
                "id": row.id,
                "action": row.action,
                "applicant": row.applicant,
                "approver": row.approver,
                "status": row.status,
                "decided_at": (
                    row.decided_at.isoformat(sep=" ", timespec="seconds") if row.decided_at else ""
                ),
            }
            for row in decided_rows
        ],
    }
