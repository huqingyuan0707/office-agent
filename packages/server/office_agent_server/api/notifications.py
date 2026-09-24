"""站内通知端点（列表 / 扫描 / 已读）——主动消息推送的站内留痕通道。

链路：端点薄封装（解析 → services/notifications → ok()）→ notifications 表。
口径：
- 列表只看本人通知（tenant+username 双重过滤，跨租户/跨人不可见）；
- 扫描是幂等批量动作（去重键保证不刷屏），可由管理员手动触发或外部定时器调用；
- 已读只影响 read_at，内容一律不改写（留痕只追加口径）。
对齐：AGENTS.md §3（分层红线）；智能办公Agent 产品需求文档.md §5.1（V1.0 主动消息推送）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_server.db import get_db
from office_agent_server.models import Notification
from office_agent_server.rbac import CurrentUser, get_current_user, require_any_perm
from office_agent_server.responses import ok
from office_agent_server.services.notification_kinds import KIND_LABELS
from office_agent_server.services.notifications import (
    list_own_notifications,
    mark_read,
    scan_notifications,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])

#: 扫描属批量管理动作（管理员/复核人可触发；外部定时器用复核员或管理员令牌）
SCAN_PERMS = ("admin", "approver")


def _to_dict(row: Notification) -> dict[str, Any]:
    """通知出参（前端消息中心直接渲染）。"""
    return {
        "id": row.id,
        "kind": row.kind,
        "kind_label": KIND_LABELS.get(row.kind, row.kind),
        "title": row.title,
        "content": row.content,
        "ref_id": row.ref_id,
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
        "read": row.read_at is not None,
        "read_at": row.read_at.isoformat(sep=" ", timespec="seconds") if row.read_at else "",
    }


@router.get("")
async def list_notifications(
    unread_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """本人通知列表（最新在前；登录即可读自己的——通知是个人数据，不做角色门槛）。"""
    result = await list_own_notifications(
        db,
        tenant=user.tenant,
        username=user.username,
        unread_only=unread_only,
        page=page,
        size=size,
    )
    return ok(
        {
            "items": [_to_dict(row) for row in result["rows"]],
            "total": result["total"],
            "page": page,
            "size": size,
        },
        "获取成功",
    )


@router.post("/scan")
async def scan(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_any_perm(*SCAN_PERMS)),
) -> dict[str, Any]:
    """扫描生成通知（审批超时/失败任务/今日简报；幂等可重复触发）。"""
    result = await scan_notifications(db, tenant=user.tenant)
    return ok(result, f"扫描完成：新建 {result['created']} 条")


@router.post("/{notification_id}/read")
async def read_notification(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """标记本人通知已读（幂等）。"""
    result = await mark_read(
        db, tenant=user.tenant, username=user.username, notification_id=notification_id
    )
    return ok(result, "已读回执成功" if not result["already_read"] else "该通知已读")
