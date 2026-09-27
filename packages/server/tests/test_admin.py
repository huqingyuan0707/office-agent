"""管理员总览端点单测（HTTP 级：聚合口径 + admin 门槛）

覆盖：admin 200 且五段齐备；无 token 401；viewer 角色 403（看板无假数据可退）；
      数据资产目录只读接口：列表分页/分类过滤/分类总览/详情 404。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.13。
"""

from __future__ import annotations

import pytest

from office_agent_server.db import session_factory
from office_agent_server.models import DataAsset, User
from office_agent_server.security import hash_password


def _login(client, username: str = "admin", password: str = "admin123") -> dict[str, str]:
    """登录取 Bearer 头（失败直接抛错：前置不满足无继续意义）。"""
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, f"登录失败：{body}"
    return {"Authorization": f"Bearer {body['data']['token']}"}


async def _ensure_viewer() -> None:
    """补一个 viewer 角色用户（种子只有 admin/reviewer；reviewer 含 admin 故不能用来验 403）。"""
    async with session_factory()() as session:
        from sqlalchemy import select

        exists = (
            await session.execute(
                select(User).where(User.tenant == "demo-tenant", User.username == "viewer1")
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                User(
                    tenant="demo-tenant",
                    username="viewer1",
                    pwd_hash=hash_password("viewer123"),
                    roles="viewer",
                    status="active",
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_overview_admin_200_with_five_sections(client) -> None:
    """admin 拿到五段聚合（users/tasks/approvals/tool_calls/recent_decisions）。"""
    resp = client.get("/api/v1/admin/overview", headers=_login(client))
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    data = body["data"]
    assert set(data) == {"users", "tasks", "approvals", "tool_calls", "recent_decisions"}
    assert data["users"]["total"] >= 2  # admin + reviewer 种子
    assert isinstance(data["tool_calls"]["top_tools"], list)
    assert isinstance(data["recent_decisions"], list)


@pytest.mark.asyncio
async def test_overview_viewer_403_without_data(client) -> None:
    """viewer 被 403 拦下（无假数据可退，前端如实提示权限不足）。"""
    await _ensure_viewer()
    resp = client.get("/api/v1/admin/overview", headers=_login(client, "viewer1", "viewer123"))
    assert resp.status_code == 403, resp.json()


def test_overview_no_token_401(client) -> None:
    """无 token 401（登录闸门在 RBAC 层，端点不重复造）。"""
    resp = client.get("/api/v1/admin/overview")
    assert resp.status_code == 401, resp.json()


def test_data_assets_list_admin_200(client) -> None:
    """admin 可查数据资产目录（含分类统计 + 分类过滤 + 分页）。"""
    resp = client.get(
        "/api/v1/admin/data-assets?page_size=3",
        headers=_login(client),
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    data = body["data"]
    assert data["total"] >= 461
    assert data["page"] == 1 and data["page_size"] == 3
    assert len(data["items"]) == 3
    assert set(data["items"][0]) == {"category", "asset_name", "kind", "description"}
    assert data["categories"][0]["name"].startswith("产品需求类")

    resp = client.get(
        "/api/v1/admin/data-assets?category=产品需求类&page_size=10",
        headers=_login(client),
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    data = body["data"]
    assert data["total"] == 22
    assert all(item["category"] == "产品需求类" for item in data["items"])


@pytest.mark.asyncio
async def test_data_assets_categories_admin_200(client) -> None:
    """分类总览对齐目录常量，并体现库表已种子行数。"""
    async with session_factory()() as session:
        session.add(
            DataAsset(
                tenant="demo-tenant",
                category="产品需求类",
                asset_name="产品需求文档PRD",
                description="测试种子行",
            )
        )
        await session.commit()
    resp = client.get("/api/v1/admin/data-assets/categories", headers=_login(client))
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    data = body["data"]
    assert data["total_categories"] == 21
    assert data["total_assets"] >= 461
    assert data["seeded_rows"] >= 1
    first = data["categories"][0]
    assert first["name"] == "产品需求类"
    assert first["catalog_count"] == 22
    assert first["seeded_count"] >= 1
    assert first["diff"] < first["catalog_count"]


def test_data_assets_detail_admin_200_and_404(client) -> None:
    """详情返回目录内容；目录中不存在的资产返回 404 信封。"""
    resp = client.get(
        "/api/v1/admin/data-assets/detail?category=Agent工程类&asset_name=系统架构文档",
        headers=_login(client),
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    assert body["data"]["asset_name"] == "系统架构文档"
    assert body["data"]["kind"] == "文档"

    resp = client.get(
        "/api/v1/admin/data-assets/detail?category=产品需求类&asset_name=不存在资产",
        headers=_login(client),
    )
    body = resp.json()
    assert resp.status_code == 404 and body.get("code") != 0, body
