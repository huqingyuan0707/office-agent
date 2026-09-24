"""管理员运营总览端点（平台使用统计看板的数据源）

链路：GET /admin/overview → require_any_perm("admin") → services/admin_stats →
      四表只读聚合 → ok()。
口径：租户内聚合只看本租户（跨租户不可见）；非 admin 角色 403（看板页如实提示权限不足，
      不降级给假数据）；本模块只做鉴权 + 调 service + ok()，不在端点内拼聚合逻辑。
对齐：AGENTS.md §3（分层红线/端点薄封装）；智能办公Agent 产品需求文档.md §2.13。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_server.db import get_db
from office_agent_server.rbac import CurrentUser, require_any_perm
from office_agent_server.responses import ok
from office_agent_server.services.admin_stats import collect_overview

router = APIRouter(prefix="/admin", tags=["admin"])

#: 运营数据属管理面：仅 admin 可见（approver/reviewer 不在其列；通配 `*` 照常放行）
ADMIN_PERMS = ("admin",)


@router.get("/overview")
async def overview(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_any_perm(*ADMIN_PERMS)),
) -> dict[str, Any]:
    """租户运营总览（用户/任务/审批计数 + 工具调用 Top + 最近裁决）。"""
    return ok(await collect_overview(db, tenant=user.tenant), "获取成功")
