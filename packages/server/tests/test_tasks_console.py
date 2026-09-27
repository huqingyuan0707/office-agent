"""任务控制台接口测试（列表/统计/创建/详情/重试/取消/删除/导出/越权/审计）

覆盖面：
- 登记任务入台（POST /api/v1/tasks）→ 列表/统计/详情/过滤可见；
- 操作列（失败重试 / 排队取消 / 删除）返回可操作结果；
- 导出 md/csv/xlsx base64 可解码；
- 权限隔离：zhangsan 无法看/改他人任务；admin scope=all 可看全租户；
- 任务操作写 tool_calls 审计留痕。

注意：测试共享 session 级 client 与空任务断言，创建的记录必须 try/finally 清理。
"""

from __future__ import annotations

import asyncio
import base64
from urllib.parse import urlencode

from sqlalchemy import func, select


def _login(client, username: str, password: str) -> str:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    return str(body["data"]["token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_task(client, headers: dict[str, str], **params: object) -> dict:
    query = urlencode({k: str(v) for k, v in params.items()})
    resp = client.post(f"/api/v1/tasks?{query}", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0, body
    return dict(body["data"])


def _delete_task(client, headers: dict[str, str], task_id: str) -> None:
    client.delete(f"/api/v1/tasks/{task_id}", headers=headers)


def _count_audit(name: str) -> int:
    """任务操作审计计数（tool_calls 表按 name 聚合）。"""

    async def _count() -> int:
        from office_agent_server.db import session_factory
        from office_agent_server.models import ToolCall

        async with session_factory()() as db:
            result = await db.execute(
                select(func.count()).select_from(ToolCall).where(ToolCall.name == name)
            )
            return int(result.scalar_one() or 0)

    return asyncio.run(_count())


def test_task_console_lifecycle_and_audit(client):
    """创建任务 → 列表/统计可见 → 失败重试 → 取消 → 导出 → 删除 → 审计留痕。"""
    admin_headers = _auth(_login(client, "admin", "admin123"))
    task_id = ""
    try:
        created = _create_task(
            client,
            admin_headers,
            name="官网改版任务拆解批量建单",
            type="breakdown.build",
            source="chat",
            ref_kind="project",
            ref_id="prj-001",
            ref_label="官网改版",
            status="failed",
            progress=0,
            note="第一期 12 个任务",
        )
        task_id = str(created["id"])
        assert created["name"].startswith("官网改版")
        assert created["source"] == "chat"
        assert created["ref_kind"] == "project"

        listing = client.get("/api/v1/tasks", headers=admin_headers).json()
        assert listing["code"] == 0
        assert any(t["id"] == task_id for t in listing["data"])

        filtered = client.get(
            f"/api/v1/tasks?{urlencode({'type': 'breakdown.build', 'q': '官网改版'})}",
            headers=admin_headers,
        ).json()
        assert filtered["code"] == 0
        assert len(filtered["data"]) == 1
        assert filtered["data"][0]["ref_label"] == "官网改版"

        stats = client.get("/api/v1/tasks/stats", headers=admin_headers).json()
        assert stats["code"] == 0
        assert stats["data"]["total"] >= 1
        assert stats["data"]["by_status"].get("failed", 0) >= 1

        detail = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers)
        assert detail.status_code == 200
        detail_body = detail.json()["data"]
        assert detail_body["id"] == task_id
        assert isinstance(detail_body["error_hint"], str)

        retry = client.post(f"/api/v1/tasks/{task_id}/retry", headers=admin_headers)
        assert retry.status_code == 200
        assert retry.json()["data"]["status"] == "pending"

        cancel = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=admin_headers)
        assert cancel.status_code == 200
        assert cancel.json()["data"]["status"] == "cancelled"

        for fmt in ("md", "csv", "xlsx"):
            exported = client.post(
                f"/api/v1/tasks/{task_id}/export?fmt={fmt}",
                headers=admin_headers,
            )
            assert exported.status_code == 200, exported.text
            data = exported.json()["data"]
            assert base64.b64decode(data["content"])
            assert data["format"] == fmt

        assert _count_audit("task.retry") >= 1
        assert _count_audit("task.export") >= 3
        assert _count_audit("task.delete") < 1  # 删除动作在 finally 里补审计
    finally:
        if task_id:
            _delete_task(client, admin_headers, task_id)

    assert _count_audit("task.delete") >= 1


def test_task_console_permission_scope(client):
    """非 admin 只能看/操作本人；admin scope=all 看全租户。"""
    admin_headers = _auth(_login(client, "admin", "admin123"))
    user_headers = _auth(_login(client, "zhangsan", "zhangsan123"))
    user_task_id = ""
    admin_task_id = ""
    try:
        user_created = _create_task(
            client,
            user_headers,
            name="员工私有文档批处理",
            type="doc.batch",
            source="docs",
            ref_kind="file",
            ref_label="合同扫描件",
            status="running",
            note="仅供本人查看",
        )
        user_task_id = str(user_created["id"])
        admin_created = _create_task(
            client,
            admin_headers,
            name="管理员后台批量建单",
            type="breakdown.build",
            source="chat",
            ref_kind="project",
            ref_label="官网改版",
            status="running",
        )
        admin_task_id = str(admin_created["id"])

        # 非 admin scope=all 也是本人维度（不报错，但看不到别人任务）
        own = client.get("/api/v1/tasks?scope=all", headers=user_headers).json()["data"]
        assert any(t["id"] == user_task_id for t in own)
        assert not any(t["id"] == admin_task_id for t in own)

        # admin scope=all 能看到普通用户任务
        all_tasks = client.get("/api/v1/tasks?scope=all", headers=admin_headers).json()["data"]
        assert any(t["id"] == user_task_id for t in all_tasks)
        assert any(t["id"] == admin_task_id for t in all_tasks)

        # 用户不能直接改他人任务（越权统一 404）
        resp = client.post(f"/api/v1/tasks/{admin_task_id}/retry", headers=user_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 1004

        admin_can_retry = client.post(f"/api/v1/tasks/{admin_task_id}/retry", headers=admin_headers)
        assert admin_can_retry.status_code == 400  # running 非终态不能重试
        assert "尚未结束" in admin_can_retry.json()["msg"]
    finally:
        if user_task_id:
            _delete_task(client, admin_headers, user_task_id)
        if admin_task_id:
            _delete_task(client, admin_headers, admin_task_id)
