"""管理员运营总览端点（平台使用统计看板 + 数据资产目录只读查询）

链路：GET /admin/overview → require_any_perm("admin") → services/admin_stats →
      四表只读聚合 → ok()；
      GET /admin/data-assets[...] → services/data_assets → data_assets 表只读查询 → ok()。
口径：租户内聚合只看本租户（跨租户不可见）；非 admin 角色 403（看板页如实提示权限不足，
      不降级给假数据）；数据资产目录对管理端只读（改目录去 data_asset_catalog.py 重跑种子）；
      本模块只做鉴权 + 调 service + ok()，不在端点内拼聚合逻辑。
对齐：AGENTS.md §3（分层红线/端点薄封装）；智能办公Agent 产品需求文档.md §2.13。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_server.db import get_db
from office_agent_server.rbac import CurrentUser, require_any_perm
from office_agent_server.responses import fail, ok
from office_agent_server.services.admin_stats import collect_overview
from office_agent_server.services.data_assets import (
    asset_detail,
    categories_overview,
    list_assets,
)

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


@router.get("/data-assets")
async def data_assets(
    category: str = Query(default="", description="按分类过滤（空=全部）"),
    page: int = Query(default=1, ge=1, description="页码，从 1 起"),
    page_size: int = Query(default=50, ge=1, le=200, description="每页条数"),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_any_perm(*ADMIN_PERMS)),
) -> dict[str, Any]:
    """数据资产目录（分类统计 + 带过滤的清单，只读不写）。"""
    return ok(
        await list_assets(
            db, tenant=user.tenant, category=category, page=page, page_size=page_size
        ),
        "获取成功",
    )


@router.get("/data-assets/categories")
async def data_assets_categories(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_any_perm(*ADMIN_PERMS)),
) -> dict[str, Any]:
    """数据资产分类总览（目录条目数 vs 库表已登记行数）。"""
    return ok(await categories_overview(db, tenant=user.tenant), "获取成功")


@router.get("/data-assets/detail")
async def data_assets_detail(
    category: str = Query(..., description="资产所属分类"),
    asset_name: str = Query(..., description="资产名称"),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_any_perm(*ADMIN_PERMS)),
) -> dict[str, Any]:
    """单个资产详情（不存在则 fail 404）。"""
    detail = await asset_detail(db, tenant=user.tenant, category=category, asset_name=asset_name)
    if detail is None:
        return fail(3204, f"资产不存在：{category}/{asset_name}", http_status=404)
    return ok(detail, "获取成功")
