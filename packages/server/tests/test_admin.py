"""管理员总览端点单测（HTTP 级：聚合口径 + admin 门槛）

覆盖：admin 200 且五段齐备；无 token 401；viewer 角色 403（看板无假数据可退）。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.13。
"""

from __future__ import annotations

import pytest

from office_agent_server.db import session_factory
from office_agent_server.models import User
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
