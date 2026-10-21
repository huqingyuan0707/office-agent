"""敏感操作实时告警（PRD §2.13：会话审计 · 敏感操作实时告警）。

链路：services/approval_flow.decide_approval（批准 → 写动作生效）→ 本模块
      → 站内通知（治理角色用户）+ IM 旁路推送（群机器人；未配置即不发网络）。

口径：
- 触发点 = 写动作**真正生效**（审批通过 + 工具执行成功）；执行失败只留失败审计，
  不发「已生效」告警——不制造虚假安全感；
- 收件人 = 本租户持治理角色（admin / approver / ``*``）的用户，**排除申请人本人**
  （申请人自己知道；告警是给治理侧的实时知情，审批人仍在收件人里因为决策要留痕）；
- 内容只带定位信息（工具 / 申请人 / 审批人 / 单号 / 参数键名），不带业务明细
  （与 IM 通道同一纪律：群消息不泄露业务内容）；
- 通知写库失败只降级记录，绝不阻断审批流与已生效的写动作。

对齐：AGENTS.md §3（分层红线：业务进 services/）；智能办公Agent 产品需求文档.md §2.13。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_server.models import User
from office_agent_server.security import split_roles
from office_agent_server.services import im_notifier
from office_agent_server.services.notification_kinds import KIND_SENSITIVE_OPERATION
from office_agent_server.services.notification_signals import add_notification

logger = logging.getLogger(__name__)

#: 治理角色（命中即收告警；``*`` 通配超级用户同样在列）
_GOVERNANCE_ROLES = ("admin", "approver", "*")

#: 参数键名最多列几个（防超长）
_MAX_KEYS = 8


async def _governance_recipients(db: AsyncSession, tenant: str, *, exclude: str) -> list[str]:
    """本租户治理角色用户（排除申请人本人）。"""
    rows = (
        await db.execute(
            select(User.username, User.roles, User.status).where(
                User.tenant == tenant, User.status == "active"
            )
        )
    ).all()
    recipients: list[str] = []
    for username, roles, _status in rows:
        if username == exclude:
            continue
        names = set(split_roles(roles))
        if names & set(_GOVERNANCE_ROLES):
            recipients.append(str(username))
    return recipients


async def alert_sensitive_operation(
    db: AsyncSession,
    *,
    tenant: str,
    tool_name: str,
    applicant: str,
    approver: str,
    approval_id: str,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """写动作生效 → 治理侧实时告警（站内通知 + IM 旁路）。

    返回 ``{"notified": [...], "im": {...}}``；IM 未配置时 im.sent=False 且 reason=not_configured
    （不发任何网络请求），站内通知失败只降级记录。
    """
    keys = [str(key) for key in (args or {})][:_MAX_KEYS]
    recipients = await _governance_recipients(db, tenant, exclude=applicant)
    bucket = []
    seen: set[tuple[str, str, str]] = set()
    for username in recipients:
        add_notification(
            bucket,
            seen,
            tenant=tenant,
            username=username,
            kind=KIND_SENSITIVE_OPERATION,
            title=f"敏感操作已生效：{tool_name}",
            content=(
                f"工具：{tool_name}\n申请人：{applicant}\n审批人：{approver}\n"
                f"审批单号：{approval_id}\n参数键：{'、'.join(keys) or '（无）'}\n"
                "写动作已生效，可在审批中心回溯明细"
            ),
            ref_id=approval_id,
        )
    if bucket:
        try:
            db.add_all(bucket)
            await db.commit()
        except Exception:  # pragma: no cover - 通知写库失败只降级
            logger.warning("sensitive operation alert write failed", exc_info=True)
            recipients = []
    im = await im_notifier.notify_sensitive_operation(
        tenant=tenant,
        tool_name=tool_name,
        applicant=applicant,
        approver=approver,
        approval_id=approval_id,
    )
    logger.info(
        "sensitive operation alert: tool=%s approval=%s notified=%d im_sent=%s",
        tool_name,
        approval_id,
        len(recipients),
        im.get("sent", False),
    )
    return {"notified": recipients, "im": im}
