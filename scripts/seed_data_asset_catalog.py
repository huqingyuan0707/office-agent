"""数据资产目录幂等种子脚本（data_assets 表 + docs/data-assets 文档）

职责：把 packages/server/office_agent_server/data_asset_catalog.py 的
      DATA_ASSET_CATALOG 灌入 data_assets 表，并重跑文档生成器，
      保证「改目录 → 重跑种子 → 库表与文档同时更新」的单一数据源闭环。

口径：幂等——已存在的 (tenant, category, asset_name) 只补 description 不重复建；
      不存在的记录补齐；目录中删除的资产不物理删行（保留历史，管理端可另加状态），
      当前只保证目录登记与文档一致。
      种子仅用于目录登记，不做任何业务数据写入。

链路：python scripts/seed_data_asset_catalog.py
      → seed_data_assets()（ORM 幂等灌库）
      → generate_data_asset_docs.main()（同步重跑文档生成器）。
红线：不触 FastAPI；不联网；description 来自 data_asset_catalog.asset_summary()
      单一函数，禁止在本脚本另写描述文案。
对齐：AGENTS.md §3（零硬编码/数据不出域）；
      智能办公Agent 产品需求文档.md §2.13（知识库后台/数据资产目录）；
      README 数据资产目录一节。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from office_agent_core.settings import settings
from office_agent_server.data_asset_catalog import (
    asset_summary,
    catalog_items,
)
from office_agent_server.db import session_factory
from office_agent_server.models import DataAsset
from scripts.generate_data_asset_docs import main as generate_docs


async def seed_data_assets(tenant: str) -> dict[str, int]:
    """幂等补齐 data_assets 目录行，返回 {created, updated, total} 统计。

    已存在行只更新 description（目录文案可能演进），不覆盖 created_at。
    """
    created = 0
    updated = 0
    total = 0
    factory = session_factory()
    async with factory() as session:
        existing = {
            (row.category, row.asset_name): row
            for row in (
                await session.execute(select(DataAsset).where(DataAsset.tenant == tenant))
            ).scalars()
        }
        for item in catalog_items():
            key = (item["category"], item["asset_name"])
            row = existing.get(key)
            if row is None:
                session.add(
                    DataAsset(
                        tenant=tenant,
                        category=item["category"],
                        asset_name=item["asset_name"],
                        description=asset_summary(item["category"], item["asset_name"]),
                    )
                )
                created += 1
            elif row.description != asset_summary(item["category"], item["asset_name"]):
                row.description = asset_summary(item["category"], item["asset_name"])
                updated += 1
        await session.commit()
        total = (
            (await session.execute(select(DataAsset).where(DataAsset.tenant == tenant)))
            .scalars()
            .all()
        )
        total = len(total)
    return {"created": created, "updated": updated, "total": total}


async def main() -> None:
    """灌库后同步重跑文档生成器，保证两份产物都反映最新目录。"""
    result = await seed_data_assets(settings.SEED_TENANT)
    print(
        f"data_assets 已幂等补齐：tenant={settings.SEED_TENANT} "
        f"created={result['created']} updated={result['updated']} total={result['total']}"
    )
    generate_docs()
    print("数据资产目录文档已同步重跑。")


if __name__ == "__main__":
    asyncio.run(main())
