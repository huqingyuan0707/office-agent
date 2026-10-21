"""任务拆解批量建单全链单测（HTTP 级：送审 → 批准 → 真实落待办 + 责任人通知）。

覆盖：提交人 invoke 落审批单 → 复核人批准 → 待办真实落盘（责任人名下）+ 站内通知；
      责任人不存在/冻结 → 批准时执行失败且不落半截台账；非建单工具不受建单校验影响。
对齐：AGENTS.md §5；智能办公Agent 产品需求文档.md §2.9、§6.5。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from office_agent_core.settings import settings
from office_agent_server.db import session_factory
from office_agent_server.models import User
from office_agent_server.security import hash_password

TENANT = "demo-tenant"


def _login(client, username: str, password: str) -> dict[str, str]:
    """登录取 Bearer 头（失败直接断言报错）。"""
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, f"登录失败：{body}"
    return {"Authorization": f"Bearer {body['data']['token']}"}


async def _ensure_user(username: str, password: str, roles: str, status: str = "active") -> None:
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
                    status=status,
                )
            )
            await session.commit()


def _store_payload() -> dict:
    """读本地事务存储（工具落盘处；server 夹具已把 DOCS_DIR 钉进临时目录）。"""
    path = Path(settings.DOCS_DIR) / "data" / "affairs.json"
    if not path.exists():
        return {"todos": [], "commits": []}
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_commit_approval_lands_todos_and_notifies_owner(client) -> None:
    """批准后：待办真实落盘（责任人名下）+ 站内通知到责任人。"""
    await _ensure_user("planner1", "planner123", "office:write,planner")
    await _ensure_user("worker1", "worker123", "office:read,worker")
    planner = _login(client, "planner1", "planner123")

    resp = client.post(
        "/api/v1/agent/tools/office.task.commit/invoke",
        json={
            "args": {
                "tasks": [
                    {"title": "需求调研与方案定稿", "owner": "worker1", "priority": "high"},
                    {"title": "前端页面开发", "owner": "worker1", "due_date": "2026-10-16"},
                ],
                "idem_key": "commit-flow-0001",
            }
        },
        headers=planner,
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    assert body["data"]["status"] == "pending_approval"
    approval_id = body["data"]["approval_id"]

    resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"reason": "同意拆解建单"},
        headers=_login(client, "admin", "admin123"),
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    execution = body["data"]["execution_result"]
    assert execution.get("status") == "ok", execution
    assert execution["result"]["count"] == 2

    payload = _store_payload()
    assert len(payload["todos"]) == 2
    assert {todo["owner"] for todo in payload["todos"]} == {"worker1"}
    assert {todo["title"] for todo in payload["todos"]} == {"需求调研与方案定稿", "前端页面开发"}

    resp = client.get("/api/v1/notifications", headers=_login(client, "worker1", "worker123"))
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    kinds = [item["kind"] for item in body["data"]["items"]]
    assert "task_assigned" in kinds
    assigned = next(item for item in body["data"]["items"] if item["kind"] == "task_assigned")
    assert "planner1" in assigned["content"]
    assert assigned["ref_id"] == "commit-flow-0001"


@pytest.mark.asyncio
async def test_commit_with_unknown_owner_fails_without_landing(client) -> None:
    """责任人不存在 → 批准时执行失败（不落半截台账，也不发通知）。"""
    await _ensure_user("planner2", "planner123", "office:write,planner")
    planner = _login(client, "planner2", "planner123")
    resp = client.post(
        "/api/v1/agent/tools/office.task.commit/invoke",
        json={
            "args": {
                "tasks": [{"title": "上线准备与发布", "owner": "查无此人"}],
                "idem_key": "commit-flow-0002",
            }
        },
        headers=planner,
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    approval_id = body["data"]["approval_id"]

    resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"reason": "同意"},
        headers=_login(client, "admin", "admin123"),
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    execution = body["data"]["execution_result"]
    assert execution.get("status") == "failed", execution
    assert "查无此人" in execution["message"]
    assert _store_payload()["todos"] == []


@pytest.mark.asyncio
async def test_commit_with_frozen_owner_fails(client) -> None:
    """责任人已冻结 → 同样拒收（只能分配给在册可用账号）。"""
    await _ensure_user("planner3", "planner123", "office:write,planner")
    await _ensure_user("frozen1", "frozen123", "office:read,worker", status="frozen")
    planner = _login(client, "planner3", "planner123")
    resp = client.post(
        "/api/v1/agent/tools/office.task.commit/invoke",
        json={
            "args": {
                "tasks": [{"title": "测试与验收", "owner": "frozen1"}],
                "idem_key": "commit-flow-0003",
            }
        },
        headers=planner,
    )
    approval_id = resp.json()["data"]["approval_id"]
    resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"reason": "同意"},
        headers=_login(client, "admin", "admin123"),
    )
    execution = resp.json()["data"]["execution_result"]
    assert execution.get("status") == "failed", execution
    assert "frozen1" in execution["message"]


@pytest.mark.asyncio
async def test_non_commit_tool_unaffected_by_owner_check(client) -> None:
    """非建单工具不受建单校验影响（precheck 一律放行）。"""
    await _ensure_user("planner4", "planner123", "office:write,planner")
    planner = _login(client, "planner4", "planner123")
    resp = client.post(
        "/api/v1/agent/tools/office.todo.create/invoke",
        json={"args": {"title": "普通待办"}},
        headers=planner,
    )
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    approval_id = body["data"]["approval_id"]
    resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"reason": "同意"},
        headers=_login(client, "admin", "admin123"),
    )
    execution = resp.json()["data"]["execution_result"]
    assert execution.get("status") == "ok", execution
