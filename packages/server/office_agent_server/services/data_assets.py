"""数据资产目录只读查询（data_assets 表 + 目录常量合并口径，纯函数不含 FastAPI 对象）。

职责：list_assets() 按租户列出目录行（可带 category 过滤），支持分页；
      list_categories() 返回分类统计（分类名 + 条目数，按目录定义顺序）。
链路：api/admin.GET /admin/data-assets → 本模块 → data_assets 表只读查询。
口径：目录行由 scripts/seed_data_asset_catalog.py 幂等灌库，管理端只读不写；
      分类/顺序以 office_agent_server.data_asset_catalog 目录常量为准，
      库表缺失的行（种子未跑）按目录常量兜底展示，prompt 提示以种子补齐。
对齐：AGENTS.md §3（分层红线：业务进 services/纯函数/数据不出域）；
      智能办公Agent 产品需求文档.md §2.13（知识库后台配置/数据资产目录）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_server.data_asset_catalog import (
    DATA_ASSET_CATALOG,
    asset_kind,
    asset_summary,
    catalog_items,
    category_intro,
)
from office_agent_server.models import DataAsset


async def list_assets(
    db: AsyncSession, *, tenant: str, category: str = "", page: int = 1, page_size: int = 50
) -> dict[str, Any]:
    """按租户列出目录行（category 可为空=全部；分页从 1 起）。

    返回 {total, page, page_size, categories, items}。
    items 每行含 {category, asset_name, kind, description}——
    kind/description 来自目录常量（单一语种/单一来源），
    库表仅存 description 快照，以常量实时计算覆盖差异（保证加减目录后无需重种即展示一致）。
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)

    def _row_from_catalog() -> list[dict[str, str]]:
        rows = []
        for item in catalog_items():
            if category and item["category"] != category:
                continue
            rows.append(
                {
                    "category": item["category"],
                    "asset_name": item["asset_name"],
                    "kind": asset_kind(item["asset_name"]),
                    "description": asset_summary(item["category"], item["asset_name"]),
                }
            )
        return rows

    catalog_rows = _row_from_catalog()
    total = len(catalog_rows)
    items = catalog_rows[(page - 1) * page_size : page * page_size]
    categories = [
        {
            "name": cat,
            "count": len(assets),
            "intro": category_intro(cat),
        }
        for cat, assets in DATA_ASSET_CATALOG.items()
    ]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "categories": categories,
        "items": items,
    }


async def asset_detail(
    db: AsyncSession, *, tenant: str, category: str, asset_name: str
) -> dict | None:
    """单个资产详情（目录常量兜底；库表行可选返回 created_at 等快照信息）。

    返回 None 表示目录中不存在该资产；存在时含 kind/description，并按库表快照补充 db 信息。
    """
    row = (
        await db.execute(
            select(DataAsset).where(
                DataAsset.tenant == tenant,
                DataAsset.category == category,
                DataAsset.asset_name == asset_name,
            )
        )
    ).scalar_one_or_none()
    for item in catalog_items():
        if item["category"] == category and item["asset_name"] == asset_name:
            return {
                "category": category,
                "asset_name": asset_name,
                "kind": asset_kind(asset_name),
                "description": asset_summary(category, asset_name),
                "in_db": row is not None,
                "db_created_at": (
                    row.created_at.isoformat(sep=" ", timespec="seconds")
                    if row is not None and row.created_at
                    else ""
                ),
            }
    return None


async def categories_overview(db: AsyncSession, *, tenant: str) -> dict[str, Any]:
    """分类总览（分类名 + 目录条目数 + 库表已登记行数，两者差异即待种量）。"""
    counts = dict(
        (
            await db.execute(
                select(DataAsset.category, func.count())
                .where(DataAsset.tenant == tenant)
                .group_by(DataAsset.category)
            )
        ).all()
    )
    return {
        "total_categories": len(DATA_ASSET_CATALOG),
        "total_assets": len(catalog_items()),
        "seeded_rows": sum(counts.values()),
        "categories": [
            {
                "name": cat,
                "catalog_count": len(assets),
                "seeded_count": int(counts.get(cat, 0)),
                "diff": max(len(assets) - int(counts.get(cat, 0)), 0),
            }
            for cat, assets in DATA_ASSET_CATALOG.items()
        ],
    }
