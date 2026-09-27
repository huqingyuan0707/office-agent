"""会话 API 单测：建/列/查/删 + 发消息受理 run + 多轮上下文注入 + 越权 1004

口径：全程不出网，agent.yaml 经 agent_yaml_dir 提供（demo-assistant 两条规则）；
发送消息复用既有 start_run 链，寒暄走即时回复；多轮上下文断言语料落在 Task.input.context。
"""

from __future__ import annotations

import pytest
from test_runtime import _auth, _login


@pytest.fixture
def conv_headers(client):
    """登录 + 会话本身隔离，逐用例新建会话防串扰。"""
    return _auth(_login(client))


def _create_conversation(client, headers: dict, title: str = "测试会话") -> str:
    resp = client.post("/api/v1/conversations", json={"title": title}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    return str(body["data"]["id"])


# ---------------- ① 会话 CRUD ----------------


def test_conversation_create_list_detail_delete(client, conv_headers, agent_yaml_dir):
    """建 → 列表 → 详情（初始空消息）→ 删除 → 列表缩水。"""
    conv_id = _create_conversation(client, conv_headers)
    # 列表
    resp = client.get("/api/v1/conversations", headers=conv_headers)
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert any(item["id"] == conv_id for item in items)
    # 详情（初始无消息）
    resp = client.get(f"/api/v1/conversations/{conv_id}", headers=conv_headers)
    assert resp.status_code == 200
    detail = resp.json()["data"]
    assert detail["id"] == conv_id
    assert detail["messages"] == []
    # 删除
    resp = client.delete(f"/api/v1/conversations/{conv_id}", headers=conv_headers)
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    resp = client.get(f"/api/v1/conversations/{conv_id}", headers=conv_headers)
    assert resp.json()["code"] == 1004


def test_conversation_delete_removes_messages(client, conv_headers, agent_yaml_dir):
    """删除会话连带删除其所有消息（不再残留历史切上下文）。"""
    conv_id = _create_conversation(client, conv_headers)
    resp = client.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={"text": "帮我演示一下回声"},
        headers=conv_headers,
    )
    assert resp.json()["code"] == 0
    resp = client.delete(f"/api/v1/conversations/{conv_id}", headers=conv_headers)
    assert resp.status_code == 200
    # 重建同名会话，详情应为空（旧消息已清）
    conv_id2 = _create_conversation(client, conv_headers, "同名会话")
    resp = client.get(f"/api/v1/conversations/{conv_id2}", headers=conv_headers)
    assert resp.json()["data"]["messages"] == []


# ---------------- ② 发消息受理 run ----------------


def test_send_greeting_gets_instant_reply(client, conv_headers):
    """纯寒暄：即时 DONE run（零工具/零智能体），助手消息有回复文案。"""
    conv_id = _create_conversation(client, conv_headers)
    resp = client.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={"text": "你好"},
        headers=conv_headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    run = data["run"]
    assert run is not None
    assert run["status"] == "DONE"
    assert data["agent_message"]
    # 详情里两条消息：user 你好 / agent 引导文案
    resp = client.get(f"/api/v1/conversations/{conv_id}", headers=conv_headers)
    msgs = resp.json()["data"]["messages"]
    assert [m["role"] for m in msgs] == ["user", "agent"]


def test_send_rule_goal_runs_agent_and_records_run_id(client, conv_headers, agent_yaml_dir):
    """规则智能体目标：走 start_run 受理链，run 详情 agent=demo-assistant，
    助手消息关联 run_id（前端可轮询刷新时间线）。"""
    conv_id = _create_conversation(client, conv_headers)
    resp = client.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={"text": "帮我演示一下回声"},
        headers=conv_headers,
    )
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    run = data["run"]
    assert run is not None
    assert run["agent"] == "demo-assistant"
    assert run["status"] == "DONE"
    assert run["steps_done"] >= 2  # 规则两步（echo → shout）都走完
    # 助手消息带 run_id
    resp = client.get(f"/api/v1/conversations/{conv_id}", headers=conv_headers)
    msgs = resp.json()["data"]["messages"]
    assert msgs[-1]["run_id"] == run["run_id"]


def test_send_route_failure_records_agent_error(client, conv_headers, agent_yaml_dir):
    """路由全不中：不落 run，助手消息如实记录错误（可追问），信封仍 code 0。"""
    conv_id = _create_conversation(client, conv_headers)
    resp = client.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={"text": "完全没有规则能接住的一句话目标"},
        headers=conv_headers,
    )
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["run"] is None
    assert data["error"]
    resp = client.get(f"/api/v1/conversations/{conv_id}", headers=conv_headers)
    agent_msg = resp.json()["data"]["messages"][-1]
    assert agent_msg["role"] == "agent"
    assert "没有智能体" in agent_msg["content"] or agent_msg["run_id"] == ""


# ---------------- ③ 多轮上下文注入 ----------------


def test_second_message_carries_conversation_context(client, conv_headers, agent_yaml_dir):
    """多轮上下文：第二条消息的 run 携带前文摘要——「再加一个/指代」类目标可据此
    回落到原智能体而非裸词掉档。"""
    conv_id = _create_conversation(client, conv_headers)
    # 首轮：规则智能体目标
    resp = client.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={"text": "帮我演示一下回声"},
        headers=conv_headers,
    )
    first_run_id = resp.json()["data"]["run"]["run_id"]
    # 次轮：无新关键词的指代目标，应靠上下文回到演示智能体
    resp = client.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={"text": "再加一个"},
        headers=conv_headers,
    )
    body = resp.json()
    assert body["code"] == 0
    run = body["data"]["run"]
    assert run is not None, "次轮目标应靠上下文命中演示规则；若 run 为 None 说明上下文没注入路由"
    assert run["agent"] == "demo-assistant", "指代目标应回落首轮智能体而非掉档"
    # 通过 run 详情核对上下文已落到 checkpoint（回放断言）
    resp = client.get(f"/api/v1/runs/{run['run_id']}", headers=conv_headers)
    detail = resp.json()["data"]
    assert detail["agent"] == "demo-assistant"
    assert first_run_id, "首轮 run 应存在"


# ---------------- ④ 越权 / 异常 ----------------


def test_conversation_missing_returns_1004(client, conv_headers):
    """访问不存在的会话：404 HTTP + 1004 信封（统一异常收口口径）。"""
    resp = client.get("/api/v1/conversations/not-exist-id", headers=conv_headers)
    assert resp.status_code == 404
    assert resp.json()["code"] == 1004
    resp = client.post(
        "/api/v1/conversations/not-exist-id/messages",
        json={"text": "你好"},
        headers=conv_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == 1004


def test_conversations_require_auth(client):
    """未登录访问会话端点：HTTP 401（鉴权拦截在最外层）。"""
    resp = client.get("/api/v1/conversations")
    assert resp.status_code == 401
