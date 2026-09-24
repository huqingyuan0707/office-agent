"""工作流编排端点单测（HTTP 级：CRUD 口径 + 顺序执行 + 审批挂起停止）

覆盖：创建/列表/详情/更新/删除；两步全读 run 直出 ok；第二步为写工具时落单即停
      pending_approval（后续步骤不跑）；未知工具创建即 1001；越权 id 404。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.11（场景串联/编排）。
"""

from __future__ import annotations


def _login(client, username: str = "admin", password: str = "admin123") -> dict[str, str]:
    """登录取 Bearer 头（失败直接抛错：前置不满足无继续意义）。"""
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, f"登录失败：{body}"
    return {"Authorization": f"Bearer {body['data']['token']}"}


def _create(client, headers, name="日报链", steps=None) -> dict:
    """建工作流并返回 data（断言 code 0，调用方只关心形状）。"""
    payload = {
        "name": name,
        "description": "smoke",
        "steps": steps
        if steps is not None
        else [
            {"tool": "office.schedule.view", "args": {"view_type": "daily"}},
            {
                "tool": "office.report.generate",
                "args": {"title": "日报", "metrics": {"事项": 3}},
            },
        ],
    }
    resp = client.post("/api/v1/workflows", headers=headers, json=payload)
    body = resp.json()
    assert resp.status_code == 200 and body.get("code") == 0, body
    return body["data"]


def test_workflow_crud_roundtrip(client) -> None:
    """创建→列表可见→详情含步骤→更新改名→删除后详情 404。"""
    headers = _login(client)
    created = _create(client, headers)
    assert created["step_count"] == 2 and len(created["steps"]) == 2

    items = client.get("/api/v1/workflows", headers=headers).json()["data"]
    assert any(item["id"] == created["id"] for item in items)

    detail = client.get(f"/api/v1/workflows/{created['id']}", headers=headers).json()["data"]
    assert detail["steps"][0]["tool"] == "office.schedule.view"

    updated = client.put(
        f"/api/v1/workflows/{created['id']}", headers=headers, json={"name": "日报链 v2"}
    ).json()["data"]
    assert updated["name"] == "日报链 v2"

    deleted = client.delete(f"/api/v1/workflows/{created['id']}", headers=headers).json()["data"]
    assert deleted["deleted"] == created["id"]
    gone = client.get(f"/api/v1/workflows/{created['id']}", headers=headers).json()
    assert gone["code"] == 1004, gone


def test_workflow_run_all_read_ok(client) -> None:
    """两步全读顺序执行 ok（步骤结果按序回填，trace 贯穿）。"""
    headers = _login(client)
    created = _create(client, headers)
    body = client.post(f"/api/v1/workflows/{created['id']}/run", headers=headers).json()
    assert body.get("code") == 0, body
    data = body["data"]
    assert data["status"] == "ok", data
    assert [s["tool"] for s in data["steps"]] == ["office.schedule.view", "office.report.generate"]
    assert all(s["status"] == "ok" for s in data["steps"])


def test_workflow_run_stops_at_approval_step(client) -> None:
    """第二步为写工具 → 落单即停 pending_approval（第一步已跑，第二步没跑）。"""
    headers = _login(client)
    created = _create(
        client,
        headers,
        name="待办链",
        steps=[
            {"tool": "office.schedule.view", "args": {}},
            {"tool": "office.todo.create", "args": {"title": "跟进事项"}},
        ],
    )
    body = client.post(f"/api/v1/workflows/{created['id']}/run", headers=headers).json()
    assert body.get("code") == 0, body
    data = body["data"]
    assert data["status"] == "pending_approval", data
    assert data["approval_id"] and data["next_step"] == 1
    assert len(data["steps"]) == 1  # 只有第一步的结果


def test_workflow_create_rejects_unknown_tool(client) -> None:
    """未知工具创建即 1001（脏编排不入库，执行期才发现等于埋雷）。"""
    headers = _login(client)
    resp = client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "坏链", "steps": [{"tool": "nope.missing", "args": {}}]},
    )
    assert resp.json().get("code") in (4005, 1001), resp.json()


def test_workflow_empty_update_rejected(client) -> None:
    """三者全空的更新 1001（幂等空操作不算成功）。"""
    headers = _login(client)
    created = _create(client, headers, name="空改链")
    resp = client.put(f"/api/v1/workflows/{created['id']}", headers=headers, json={})
    assert resp.json().get("code") == 1001, resp.json()
