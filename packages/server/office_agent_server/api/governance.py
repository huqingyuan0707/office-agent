"""治理状态端点（运维一眼看清「为什么工具调不动」）

链路：GET /governance/status → 注册中心 + 联动层 + 可观测 + 审批待办 → 单页聚合。

为什么要这个端点：跨系统联动最贵的排障成本是「工具在清单里，但一调就失败」。
本端点把「哪些上游提供方已配置 / 哪些被工具引用却还没配」直接摊开，
再叠加执行侧的可用率指标，让运维不用翻日志猜。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from office_agent_core import audit, linkage, registry
from office_agent_core.observability import snapshot as metrics_snapshot
from office_agent_core.settings import settings
from office_agent_server.db import get_db
from office_agent_server.models import Approval
from office_agent_server.rbac import CurrentUser, get_current_user
from office_agent_server.responses import ok

router = APIRouter(prefix="/governance", tags=["governance"])


def _provider_views() -> list[dict[str, Any]]:
    """已配置的上游提供方（只回地址，绝不回令牌）。"""
    views: list[dict[str, Any]] = []
    for provider_id in linkage.configured_ids():
        provider = linkage.get_provider(provider_id)
        views.append(
            {
                "provider_id": provider_id,
                "endpoint": provider.endpoint if provider is not None else "",
            }
        )
    return views


def _missing_providers() -> list[str]:
    """被工具引用但尚未配置的提供方（这些工具一定会失败，先暴露出来）。"""
    wanted = {
        spec.remote.provider_id
        for spec in registry.all_specs()
        if spec.remote is not None and not linkage.configured(spec.remote.provider_id)
    }
    return sorted(wanted)


@router.get("/status")
async def status(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """治理状态总览（注册中心 / 联动 / 审计 / 审批 / 指标）。"""
    specs = registry.all_specs()
    remote = sum(1 for spec in specs if spec.is_remote)
    pending = (
        await db.execute(
            select(func.count())
            .select_from(Approval)
            .where(Approval.tenant == user.tenant, Approval.status == "pending")
        )
    ).scalar_one()
    return ok(
        {
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "env": settings.ENV,
            "tools": {"total": len(specs), "remote": remote, "local": len(specs) - remote},
            "providers": _provider_views(),
            "providers_missing": _missing_providers(),
            "pending_approvals": int(pending),
            "audit_recent": audit.count(),
            "metrics": metrics_snapshot(),
        },
        "获取成功",
    )
