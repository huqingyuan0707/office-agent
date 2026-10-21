"""任务拆解批量建单的宿主侧编排（PRD §2.9 / §6.5）：责任人校验 + 站内通知。

链路：services/approval_flow.decide_approval（批准 → 以提交人身份执行）→ 本模块
      precheck()（执行前校验）→ executor.call（工具落待办）→ after_commit()（通知责任人）。

职责：
- precheck()：责任人必须是本租户真实且状态正常的用户（PRD §6.5.2「只能分配当前用户
  有权限操作的人员」）；不存在/已冻结 → 执行前拒收，不落半截台账；
- after_commit()：建单成功后给每个责任人生成一条站内通知（去重 ref_id=idem_key，
  一个建单批次每人一条，绝不刷屏）。

分层口径：工具侧（tools-office）只落本地待办存储与幂等台账，**不 import server 包**
（依赖方向 server→tools-office）；用户与通知表属宿主域，故校验与通知在本模块。
红线：非 office.task.commit 一律放行（不误伤其它工具）；通知失败只降级不阻断审批流。
对齐：AGENTS.md §3（分层红线：业务进 services/）；智能办公Agent 产品需求文档.md §6.5。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_server.models import Notification, User
from office_agent_server.services.notification_kinds import KIND_TASK_ASSIGNED
from office_agent_server.services.notification_signals import add_notification

logger = logging.getLogger(__name__)

#: 批量建单工具名（其它工具一律放行）
COMMIT_TOOL = "office.task.commit"

#: 单次建单通知里最多列出的任务条数（防刷屏；超出只报总数）
_MAX_LISTED = 5


def _owners_of(args: dict[str, Any]) -> list[str]:
    """入参里的责任人人名（去重、保序）。"""
    tasks = args.get("tasks")
    if not isinstance(tasks, list):
        return []
    owners: list[str] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        owner = str(item.get("owner") or "").strip()
        if owner and owner not in owners:
            owners.append(owner)
    return owners


async def precheck(
    db: AsyncSession, *, tenant: str, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    """建单前置校验（执行前）：责任人必须是本租户在用的账号。

    返回 ``{"ok": bool, "owners": [...], "message": str}``；
    非 office.task.commit 一律 ok（不误伤其它工具）。
    """
    if tool != COMMIT_TOOL:
        return {"ok": True, "owners": []}
    owners = _owners_of(args)
    if not owners:
        return {"ok": True, "owners": []}
    rows = (
        await db.execute(
            select(User.username, User.status).where(
                User.tenant == tenant, User.username.in_(owners)
            )
        )
    ).all()
    status_of = {str(row[0]): str(row[1]) for row in rows}
    unknown = [owner for owner in owners if owner not in status_of]
    frozen = [owner for owner in owners if status_of.get(owner) == "frozen"]
    if unknown or frozen:
        parts = []
        if unknown:
            parts.append("不存在：" + "、".join(unknown))
        if frozen:
            parts.append("已冻结：" + "、".join(frozen))
        return {
            "ok": False,
            "owners": owners,
            "message": f"以下责任人{'；'.join(parts)}——只能分配给本租户在用的账号，请先指派在册人员",
        }
    return {"ok": True, "owners": owners}


async def after_commit(
    db: AsyncSession,
    *,
    tenant: str,
    tool: str,
    result: dict[str, Any],
    applicant: str,
) -> dict[str, Any]:
    """建单成功后：给责任人生成站内通知（去重 ref_id=idem_key）。

    返回 ``{"notified": [责任人...]}``；非建单工具或无责任人不做任何事。
    通知写库失败只降级记录（绝不阻断审批流与已生效的建单）。
    """
    if tool != COMMIT_TOOL:
        return {"notified": []}
    owners = [str(owner) for owner in (result.get("notified_owners") or []) if owner]
    if not owners:
        return {"notified": []}
    created = [row for row in (result.get("created") or []) if isinstance(row, dict)]
    ref_id = str(result.get("idem_key") or "")[:64]
    bucket: list[Notification] = []
    seen: set[tuple[str, str, str]] = set()
    for owner in owners:
        titles = [
            str(row.get("title") or "") for row in created if str(row.get("owner") or "") == owner
        ]
        listed = titles[:_MAX_LISTED]
        tail = f"（另有 {len(titles) - len(listed)} 条）" if len(titles) > len(listed) else ""
        content = (
            f"{applicant} 给你指派了 {len(titles)} 项任务：\n"
            + "\n".join(f"{index}. {title}" for index, title in enumerate(listed, 1))
            + tail
            + "\n请到待办清单查看并按期完成"
        )
        add_notification(
            bucket,
            seen,
            tenant=tenant,
            username=owner,
            kind=KIND_TASK_ASSIGNED,
            title="收到新任务指派",
            content=content,
            ref_id=ref_id,
        )
    if not bucket:
        return {"notified": []}
    try:
        db.add_all(bucket)
        await db.commit()
    except Exception:  # pragma: no cover - 通知写库失败只降级
        logger.warning("task assign notification write failed", exc_info=True)
        return {"notified": []}
    logger.info("task assign notified: idem_key=%s owners=%s", ref_id, ",".join(owners))
    return {"notified": owners}
