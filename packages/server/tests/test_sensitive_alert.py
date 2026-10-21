"""敏感操作实时告警单测（PRD §2.13）：写动作生效才告警 + 收件人口径 + IM 旁路。

覆盖：批准写动作生效 → 治理角色用户收到站内敏感操作告警（申请人本人不收）；
      执行失败 → 不产生「已生效」告警（失败只留审计）；
      IM 未配置 → 如实 not_configured 且不发网络；已配置 → 消息体只带定位信息。
对齐：AGENTS.md §5；智能办公Agent 产品需求文档.md §2.13。
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from office_agent_core.settings import settings
from office_agent_server.db import session_factory
from office_agent_server.models import Notification, User
from office_agent_server.security import hash_password
from office_agent_server.services import im_notifier

TENANT = "demo-tenant"


def _login(client, username: str, password: str) -> dict[str, str]:
    """登录取 Bearer 头（失败直接断言报错）。"""
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, f"登录失败：{body}"
    return {"Authorization": f"Bearer {body['data']['token']}"}


async def _ensure_user(username: str, password: str, roles: str) -> None:
    """幂等建测试用户（角色串逗号分隔）。"""
    async with session_factory()() as session:
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


async def _notifies_of(username: str, kind: str, ref_id: str = "") -> list[Notification]:
    """取某用户的某类通知（数据库直查，避开分页与顺序干扰；ref_id 可再收窄）。"""
    async with session_factory()() as session:
        stmt = select(Notification).where(
            Notification.tenant == TENANT,
            Notification.username == username,
            Notification.kind == kind,
        )
        if ref_id:
            stmt = stmt.where(Notification.ref_id == ref_id)
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)


@pytest.mark.asyncio
async def test_effective_write_action_alerts_governance_users(client) -> None:
    """写动作生效 → 治理角色用户收到敏感操作告警；申请人本人不收。"""
    await _ensure_user("actor1", "actor123", "office:write,actor")
    actor = _login(client, "actor1", "actor123")
    resp = client.post(
        "/api/v1/agent/tools/office.todo.create/invoke",
        json={"args": {"title": "敏感操作告警用例待办"}},
        headers=actor,
    )
    approval_id = resp.json()["data"]["approval_id"]
    resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"reason": "同意"},
        headers=_login(client, "admin", "admin123"),
    )
    body = resp.json()
    assert body["data"]["execution_result"]["status"] == "ok", body

    alerts = await _notifies_of("admin", "sensitive_operation")
    assert alerts, "治理角色用户应收到敏感操作告警"
    alert = alerts[-1]
    assert alert.ref_id == approval_id
    assert "office.todo.create" in alert.title
    assert "actor1" in alert.content

    assert await _notifies_of("actor1", "sensitive_operation") == [], "申请人本人不收告警"


@pytest.mark.asyncio
async def test_failed_execution_produces_no_effective_alert(client) -> None:
    """执行失败 → 不产生「已生效」告警（失败只留审计，不制造虚假安全感）。"""
    await _ensure_user("actor2", "actor123", "office:write,actor")
    actor = _login(client, "actor2", "actor123")
    resp = client.post(
        "/api/v1/agent/tools/office.task.commit/invoke",
        json={
            "args": {
                "tasks": [{"title": "责任人不存在用例", "owner": "查无此人"}],
                "idem_key": "alert-fail-0001",
            }
        },
        headers=actor,
    )
    approval_id = resp.json()["data"]["approval_id"]
    resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"reason": "同意"},
        headers=_login(client, "admin", "admin123"),
    )
    body = resp.json()
    assert body["data"]["execution_result"]["status"] == "failed", body
    assert await _notifies_of("admin", "sensitive_operation", ref_id=approval_id) == []


@pytest.mark.asyncio
async def test_im_channel_not_configured_reports_honestly() -> None:
    """IM 未配置 → 如实 not_configured（不发网络、不抛错）。"""
    original = settings.IM_WEBHOOK_URL
    settings.IM_WEBHOOK_URL = ""
    try:
        result = await im_notifier.notify_sensitive_operation(
            tenant=TENANT,
            tool_name="office.todo.create",
            applicant="actor1",
            approver="admin",
            approval_id="approval-1",
        )
    finally:
        settings.IM_WEBHOOK_URL = original
    assert result["sent"] is False
    assert result["reason"] == "not_configured"


@pytest.mark.asyncio
async def test_im_channel_message_carries_locator_only() -> None:
    """IM 消息体只带定位信息（工具/申请人/审批人/单号），不带业务明细。"""
    original = (settings.IM_WEBHOOK_URL, settings.IM_WEBHOOK_TYPE)
    settings.IM_WEBHOOK_URL = "https://im.test/hook"
    settings.IM_WEBHOOK_TYPE = "generic"
    sent: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"code": 0})

    im_notifier.set_transport(httpx.MockTransport(_handler))
    try:
        result = await im_notifier.notify_sensitive_operation(
            tenant=TENANT,
            tool_name="office.task.commit",
            applicant="actor1",
            approver="admin",
            approval_id="approval-2",
        )
    finally:
        im_notifier.set_transport(None)
        settings.IM_WEBHOOK_URL, settings.IM_WEBHOOK_TYPE = original

    assert result["sent"] is True, result
    payload = json.loads(sent[-1].content)
    assert payload["kind"] == "sensitive_operation"
    assert "office.task.commit" in payload["content"]
    assert "approval-2" in payload["content"]
