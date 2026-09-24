"""通知端点单测：扫描幂等 / 按人隔离 / 已读回执 / 失败任务与审批超时提醒 / RBAC

链路：TestClient(lifespan) → /api/v1/notifications → services/notifications → notifications 表。
夹具：tests/conftest.py（session 级 client + 临时库）；种子账号 admin/reviewer；
      clerk 等特殊账号由用例经独立引擎直插（与审计测试同模式）。
对齐：AGENTS.md §2/§3（端点薄封装 + services 分层）；智能办公Agent 产品需求文档.md §5.1
      （V1.0 主动消息推送）、§2.2（审批超时预警/任务失败提醒/定期简报）。
"""

from __future__ import annotations


def _login(client, username: str = "admin", password: str = "admin123") -> str:
    """登录取 token（种子账号由 lifespan 建好）。"""
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    return str(body["data"]["token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_notifications_scan_is_idempotent(client):
    """扫描可重复触发：同键只建一次，二次扫描 created=0 且 skipped 与首扫新建数一致。

    站内通知靠 (tenant, username, kind, ref_id) 唯一去重——外部定时器反复调 scan 绝不刷屏。
    """
    admin = _auth(_login(client))
    first = client.post("/api/v1/notifications/scan", headers=admin).json()
    assert first["code"] == 0
    data = first["data"]
    assert data["created"] >= 1  # 至少每名活跃用户一条今日简报
    assert data["by_kind"]["daily_briefing"] >= 1

    second = client.post("/api/v1/notifications/scan", headers=admin).json()
    assert second["code"] == 0
    assert second["data"]["created"] == 0
    assert second["data"]["skipped_existing"] == data["created"]


def test_notifications_list_only_mine_and_read_receipt(client):
    """列表按人隔离（tenant+username 双过滤）；已读回执幂等，未读过滤生效。"""
    admin = _auth(_login(client))
    reviewer = _auth(_login(client, "reviewer", "reviewer123"))
    client.post("/api/v1/notifications/scan", headers=admin)  # 幂等：有则跳过

    r_items = client.get("/api/v1/notifications", headers=reviewer).json()["data"]
    assert r_items["total"] >= 1
    assert all(item["kind"] == "daily_briefing" for item in r_items["items"])
    assert all(item["kind_label"] == "今日简报" for item in r_items["items"])

    unread_before = client.get(
        "/api/v1/notifications", params={"unread_only": "true"}, headers=reviewer
    ).json()["data"]["total"]
    target = r_items["items"][0]["id"]

    read_once = client.post(f"/api/v1/notifications/{target}/read", headers=reviewer).json()
    assert read_once["code"] == 0
    assert read_once["data"]["already_read"] is False
    assert read_once["data"]["read_at"]

    read_twice = client.post(f"/api/v1/notifications/{target}/read", headers=reviewer).json()
    assert read_twice["data"]["already_read"] is True  # 幂等：重复已读不报错

    unread_after = client.get(
        "/api/v1/notifications", params={"unread_only": "true"}, headers=reviewer
    ).json()["data"]["total"]
    assert unread_after == unread_before - 1

    missing = client.post("/api/v1/notifications/doesnotexist/read", headers=reviewer)
    assert missing.status_code == 404
    assert missing.json()["code"] == 1004


async def test_failed_task_notification_is_generated_once(client):
    """失败任务提醒：status=failed → 提醒任务提交人；重复扫描只提醒一次。

    失败任务行由独立引擎直插（不与测试客户端所在事件循环共享连接池）。
    """
    import uuid

    from sqlalchemy import insert
    from sqlalchemy.ext.asyncio import create_async_engine

    from office_agent_core.settings import settings
    from office_agent_server.models import Task

    task_id = uuid.uuid4().hex
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                insert(Task).values(
                    id=task_id,
                    tenant="demo-tenant",
                    username="admin",
                    type="demo_job",
                    status="failed",
                    error="网络超时：上游无响应",
                )
            )
    finally:
        await engine.dispose()

    admin = _auth(_login(client))
    body = client.post("/api/v1/notifications/scan", headers=admin).json()
    assert body["code"] == 0
    assert body["data"]["by_kind"]["task_failed"] == 1

    again = client.post("/api/v1/notifications/scan", headers=admin).json()
    assert again["data"]["created"] == 0  # 同一失败任务只提醒一次

    items = client.get("/api/v1/notifications", headers=admin).json()["data"]["items"]
    failed = next(item for item in items if item["kind"] == "task_failed")
    assert failed["ref_id"] == task_id
    assert "网络超时" in failed["content"]


def test_approval_stale_notification(client, monkeypatch):
    """审批超时提醒：stale_hours=0 时 pending 单立即判定超时，提醒提交人且 ref_id=审批单 id。"""
    from office_agent_core.settings import settings

    admin = _auth(_login(client))
    resp = client.post(
        "/api/v1/agent/tools/office.todo.create/invoke",
        json={"args": {"title": "超时提醒回归", "priority": "low"}},
        headers=admin,
    ).json()
    assert resp["code"] == 0
    approval_id = resp["data"]["approval_id"]

    monkeypatch.setattr(settings, "APPROVAL_STALE_HOURS", 0)  # 刚建的单立即判超时
    body = client.post("/api/v1/notifications/scan", headers=admin).json()
    assert body["code"] == 0
    assert body["data"]["by_kind"]["approval_stale"] == 1

    items = client.get("/api/v1/notifications", headers=admin).json()["data"]["items"]
    stale = next(item for item in items if item["kind"] == "approval_stale")
    assert stale["ref_id"] == approval_id
    assert stale["kind_label"] == "审批超时提醒"


async def test_notification_scan_requires_admin_or_approver(client):
    """非 admin/approver 触发扫描 → 403（信封 1003）；普通用户只能看自己的通知。"""
    from sqlalchemy import insert
    from sqlalchemy.ext.asyncio import create_async_engine

    from office_agent_core.settings import settings
    from office_agent_server.models import User
    from office_agent_server.security import hash_password

    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                insert(User).values(
                    tenant="demo-tenant",
                    username="clerk",
                    pwd_hash=hash_password("viewer123"),
                    roles="viewer",
                )
            )
    finally:
        await engine.dispose()

    clerk = _auth(_login(client, "clerk", "viewer123"))
    resp = client.post("/api/v1/notifications/scan", headers=clerk)
    assert resp.status_code == 403
    assert resp.json()["code"] == 1003

    # clerk 能看自己的通知列表（此时为空，绝不看到他人通知）
    mine = client.get("/api/v1/notifications", headers=clerk).json()
    assert mine["code"] == 0
    assert mine["data"]["total"] == 0
    assert mine["data"]["items"] == []
