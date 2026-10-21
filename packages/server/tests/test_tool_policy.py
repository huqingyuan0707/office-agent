"""细粒度权限（角色 × 工具启停）单测（HTTP 级）

覆盖：admin 策略管理端点（清单/设置/删除）+ 角色与工具入参校验；
      禁用后工具清单不再出现、invoke 被 4006 拦下；通配超级用户豁免；
      无策略时零回归；审批放行时按提交人当前策略复核（批准不等于绕过新策略）。

对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.11「工具细粒度权限管控」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from office_agent_server.db import session_factory
from office_agent_server.models import ToolPolicy, User
from office_agent_server.security import hash_password

TENANT = "demo-tenant"


def _login(client, username: str, password: str) -> dict[str, str]:
    """登录取 Bearer 头（失败直接断言报错）。"""
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, f"登录失败：{body}"
    return {"Authorization": f"Bearer {body['data']['token']}"}


async def _ensure_user(username: str, password: str, roles: str) -> None:
    """幂等建测试用户（角色串逗号分隔；角色名同时充当策略的匹配键）。"""
    async with session_factory()() as session:
        from sqlalchemy import select

        exists = (
            await session.execute(
                select(User).where(User.tenant == TENANT, User.username == username)
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                User(
                    tenant=TENANT,
                    username=username,
                    pwd_hash=hash_password(password),
                    roles=roles,
                    status="active",
                )
            )
            await session.commit()


@pytest.fixture(autouse=True)
async def _clean_policies():
    """逐用例清空策略表：策略是全局口径，前序用例的禁用不能连坐后序用例。"""
    async with session_factory()() as session:
        await session.execute(delete(ToolPolicy).where(ToolPolicy.tenant == TENANT))
        await session.commit()
    yield
    async with session_factory()() as session:
        await session.execute(delete(ToolPolicy).where(ToolPolicy.tenant == TENANT))
        await session.commit()


@pytest.mark.asyncio
async def test_tool_policies_admin_200_with_roles_and_tools(client) -> None:
    """admin 拿到策略清单 + 可选角色 + 可选工具目录；非 admin 403。"""
    resp = client.get("/api/v1/admin/tool-policies", headers=_login(client, "admin", "admin123"))
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    data = body["data"]
    assert data["items"] == []
    assert "approver" in data["roles"] and "office:read" in data["roles"]
    assert any(item["name"] == "office.todo.list" for item in data["tools"])

    await _ensure_user("viewer1", "viewer123", "viewer")
    resp = client.get("/api/v1/admin/tool-policies", headers=_login(client, "viewer1", "viewer123"))
    assert resp.status_code == 403, resp.json()


def test_tool_policy_validation_rejects_unknown_role_and_tool(client) -> None:
    """角色必须真实存在（拼错即 1001），工具必须已注册（否则 4005）。"""
    headers = _login(client, "admin", "admin123")
    resp = client.post(
        "/api/v1/admin/tool-policies",
        json={"role": "不存在的角色", "tool_name": "office.todo.list", "enabled": False},
        headers=headers,
    )
    body = resp.json()
    assert body.get("code") == 1001, body

    resp = client.post(
        "/api/v1/admin/tool-policies",
        json={"role": "viewer", "tool_name": "office.not.exists", "enabled": False},
        headers=headers,
    )
    body = resp.json()
    assert body.get("code") == 4005, body


@pytest.mark.asyncio
async def test_disabled_tool_hidden_from_list_and_invoke_denied(client) -> None:
    """禁用后：清单里没有该工具、invoke 直接 4006（不是「能看见但调不动」）。"""
    await _ensure_user("staff1", "staff123", "office:read,staff")
    headers = _login(client, "admin", "admin123")
    resp = client.post(
        "/api/v1/admin/tool-policies",
        json={"role": "staff", "tool_name": "office.todo.list", "enabled": False},
        headers=headers,
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    assert body["data"]["enabled"] is False

    staff = _login(client, "staff1", "staff123")
    resp = client.get("/api/v1/agent/tools", headers=staff)
    body = resp.json()
    names = [item["name"] for item in body["data"]["items"]]
    assert "office.todo.list" not in names
    assert "office.schedule.view" in names  # 同角色其他工具不受影响

    resp = client.post(
        "/api/v1/agent/tools/office.todo.list/invoke", json={"args": {}}, headers=staff
    )
    body = resp.json()
    assert body.get("code") == 4006, body
    assert "禁用" in body["msg"]


@pytest.mark.asyncio
async def test_wildcard_superuser_bypasses_policy(client) -> None:
    """通配超级用户（roles 含 *）豁免策略：管理员不会被自己的策略锁死。"""
    await _ensure_user("staff2", "staff123", "office:read,staff")
    headers = _login(client, "admin", "admin123")
    resp = client.post(
        "/api/v1/admin/tool-policies",
        json={"role": "staff", "tool_name": "office.todo.list", "enabled": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    admin = _login(client, "admin", "admin123")
    resp = client.get("/api/v1/agent/tools", headers=admin)
    names = [item["name"] for item in resp.json()["data"]["items"]]
    assert "office.todo.list" in names


@pytest.mark.asyncio
async def test_global_policy_applies_to_all_non_wildcard_users(client) -> None:
    """role="*" 的策略对所有非通配用户生效（全局禁用一个危险工具的口子）。"""
    headers = _login(client, "admin", "admin123")
    resp = client.post(
        "/api/v1/admin/tool-policies",
        json={"role": "*", "tool_name": "office.todo.list", "enabled": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()

    resp = client.get("/api/v1/agent/tools", headers=_login(client, "zhangsan", "zhangsan123"))
    names = [item["name"] for item in resp.json()["data"]["items"]]
    assert "office.todo.list" not in names


@pytest.mark.asyncio
async def test_delete_policy_restores_default_allow(client) -> None:
    """删除策略 = 回到默认放行（清单恢复、invoke 恢复）。"""
    await _ensure_user("staff3", "staff123", "office:read,staff")
    admin = _login(client, "admin", "admin123")
    client.post(
        "/api/v1/admin/tool-policies",
        json={"role": "staff", "tool_name": "office.todo.list", "enabled": False},
        headers=admin,
    )
    staff = _login(client, "staff3", "staff123")
    resp = client.post(
        "/api/v1/agent/tools/office.todo.list/invoke", json={"args": {}}, headers=staff
    )
    assert resp.json().get("code") == 4006, resp.json()

    resp = client.request(
        "DELETE",
        "/api/v1/admin/tool-policies?role=staff&tool_name=office.todo.list",
        headers=admin,
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    assert body["data"]["deleted"] is True

    resp = client.get("/api/v1/agent/tools", headers=staff)
    names = [item["name"] for item in resp.json()["data"]["items"]]
    assert "office.todo.list" in names


@pytest.mark.asyncio
async def test_no_policy_keeps_tool_list_unchanged(client) -> None:
    """零回归：无任何策略时，工具清单与注册中心一致。"""
    from office_agent_core import registry

    resp = client.get("/api/v1/agent/tools", headers=_login(client, "zhangsan", "zhangsan123"))
    body = resp.json()
    listed = {item["name"] for item in body["data"]["items"]}
    registered = {spec.name for spec in registry.all_specs()}
    assert registered.issubset(listed) or listed.issubset(registered)
    assert "office.todo.list" in listed


@pytest.mark.asyncio
async def test_approval_execution_rechecks_current_policy(client) -> None:
    """审批放行时按提交人当前策略复核：申请后被禁用 → 批准即生效失败（状态仍留痕）。"""
    await _ensure_user("writer1", "writer123", "office:write,writer")
    writer = _login(client, "writer1", "writer123")
    resp = client.post(
        "/api/v1/agent/tools/office.todo.create/invoke",
        json={"args": {"title": "细粒度权限复核用例"}},
        headers=writer,
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    approval_id = body["data"]["approval_id"]

    admin = _login(client, "admin", "admin123")
    resp = client.post(
        "/api/v1/admin/tool-policies",
        json={"role": "writer", "tool_name": "office.todo.create", "enabled": False},
        headers=admin,
    )
    assert resp.status_code == 200, resp.json()

    resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve", json={"reason": "复核放行"}, headers=admin
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    execution = body["data"]["execution_result"]
    assert execution.get("status") == "failed", execution
    assert execution.get("code") == 4006, execution
