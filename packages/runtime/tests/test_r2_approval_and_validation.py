"""R2 单测：审批闭环（MockTransport / 不打真实网络）+ 回答数值校验

链路：
① 审批闭环：todo.create 步 → 送审挂起（WAITING_APPROVAL）→ 复核员批准 →
   GET 内联 lazy resume 续跑成功（DONE）；驳回 → run 收敛 FAILED 终态留痕，
   显式续跑拒绝（4004）。审批单走 server 侧 services.approval_flow（既有送审机制），
   同人红线（审批人 != 提交人）由 reviewer 种子账号保证。
② 数值校验：LLM 终答经 validate_answer_numbers 校验——通过（数字可溯源）与
   未通过（编造数字 → 标注失信 + 附原始数据，不删回答）两分支；纯函数直测
   含日期字符串数字。

口径：全程不出网（LLM 用 httpx.MockTransport；工具全本地 handler）；agent.yaml
      经 tmp_path + monkeypatch loader.agents_dir 提供，不写仓库 plugins/。
对齐：.trae/documents/智能体编排层实现方案.md §R2；skills/doubao-coding-develop-unit-tests
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from test_runtime import _auth, _login

from office_agent_runtime.validation import validate_answer_numbers


def _login_reviewer(client) -> str:
    """复核员登录取 token（种子账号 reviewer/reviewer123，角色 admin+approver）。"""
    resp = client.post(
        "/api/v1/auth/login", json={"username": "reviewer", "password": "reviewer123"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    return str(body["data"]["token"])


def _yaml_dir(tmp_path, dirname: str, text: str):
    """写一份 agent.yaml 到临时 plugins 目录（不污染仓库 plugins/）。"""
    root = tmp_path / "plugins"
    target = root / dirname
    target.mkdir(parents=True)
    (target / "agent.yaml").write_text(text, encoding="utf-8")
    return root


_APPROVAL_YAML = """\
name: approval-assistant
description: 审批闭环演示：回声 → 建待办（office.todo.create 恒送审）
tools: [demo.echo, office.todo.create]
max_steps: 4
rules:
  - match: ["待办"]
    steps:
      - tool: demo.echo
        args: {text: "周报"}
      - tool: office.todo.create
        args: {title: "写{steps[0].result.echo.text}", priority: "high"}
"""


def _start_approval_run(client, token: str, tmp_path) -> str:
    """发起会触发送审的 run，返回 run_id（断言挂起态 + pending 时间线）。"""
    from office_agent_runtime import loader

    loader_monkey = _yaml_dir(tmp_path, "approval-assistant", _APPROVAL_YAML)
    _patch_loader(loader, loader_monkey)
    body = client.post(
        "/api/v1/runs",
        json={"agent": "approval-assistant", "goal": "帮我建个待办"},
        headers=_auth(token),
    ).json()
    assert body["code"] == 0, body
    assert body["data"]["status"] == "WAITING_APPROVAL", body["data"]
    return str(body["data"]["run_id"])


def _patch_loader(loader, root) -> None:
    """monkeypatch loader.agents_dir 指向临时目录（沿用既有测试手法）。"""
    from pytest import MonkeyPatch

    MonkeyPatch().setattr(loader, "agents_dir", lambda: root)


# ---------------- ① 审批闭环：挂起 → 批准 → lazy resume 续跑成功 ----------------


def test_approval_suspend_then_approve_resumes_to_done(client, tmp_path):
    token = _login(client)
    run_id = _start_approval_run(client, token, tmp_path)

    # 挂起快照：第 2 步 pending、审批单号落时间线与 checkpoint
    detail = client.get(f"/api/v1/runs/{run_id}", headers=_auth(token)).json()["data"]
    assert detail["status"] == "WAITING_APPROVAL"
    assert detail["pending_approval"]["tool"] == "office.todo.create"
    assert [step["status"] for step in detail["steps"]] == ["ok", "pending"]
    approval_id = str(detail["pending_approval"]["approval_id"])
    assert approval_id

    # 审批中心可见该单（待办）
    reviewer_token = _login_reviewer(client)
    pending = client.get("/api/v1/approvals", headers=_auth(reviewer_token)).json()["data"]
    assert any(item["id"] == approval_id for item in pending)

    # 复核员批准（审批人 != 提交人 admin，同人红线不触发）
    approved = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"reason": "同意创建"},
        headers=_auth(reviewer_token),
    ).json()
    assert approved["code"] == 0 and approved["data"]["status"] == "approved"

    # GET 内联裁决：approved → lazy resume 续跑 → DONE
    resumed = client.get(f"/api/v1/runs/{run_id}", headers=_auth(token)).json()["data"]
    assert resumed["status"] == "DONE", resumed
    assert "审批已通过" in str(resumed.get("approval_hint") or "")
    # 时间线留痕：ok(0) → pending(1) → 重放 ok(1)，pending 行保留审批单号可对账
    assert [(s["step_index"], s["status"]) for s in resumed["steps"]] == [
        (0, "ok"),
        (1, "pending"),
        (1, "ok"),
    ]
    replayed = resumed["steps"][2]
    assert replayed["result"]["title"] == "写周报"  # 取值模板在续跑回放下生效
    assert replayed["result"]["priority"] == "high"
    assert resumed["error"] == ""


# ---------------- ② 审批闭环：驳回 → run 终态留痕，续跑拒绝 ----------------


def test_approval_rejected_finalizes_run_and_blocks_resume(client, tmp_path):
    token = _login(client)
    run_id = _start_approval_run(client, token, tmp_path)
    reviewer_token = _login_reviewer(client)
    detail = client.get(f"/api/v1/runs/{run_id}", headers=_auth(token)).json()["data"]
    approval_id = str(detail["pending_approval"]["approval_id"])

    rejected = client.post(
        f"/api/v1/approvals/{approval_id}/reject",
        json={"reason": "待办内容不合规"},
        headers=_auth(reviewer_token),
    ).json()
    assert rejected["code"] == 0

    # GET 内联裁决：rejected → run 收敛 FAILED，时间线留 rejected 行（不删留痕）
    data = client.get(f"/api/v1/runs/{run_id}", headers=_auth(token)).json()["data"]
    assert data["status"] == "FAILED"
    assert "驳回" in data["error"]
    assert [(s["step_index"], s["status"]) for s in data["steps"]] == [
        (0, "ok"),
        (1, "pending"),
        (1, "rejected"),
    ]
    assert "审批驳回" in data["steps"][2]["result"]

    # 驳回终态拒绝续跑（审批否决不可绕过：4004）
    resp = client.post(f"/api/v1/runs/{run_id}/resume", headers=_auth(token))
    assert resp.status_code == 400
    assert resp.json()["code"] == 4004


# ---------------- ③ 数值校验：LLM 终答两分支（MockTransport 假 LLM） ----------------


def _chat_text(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


_ANSWER_YAML = """\
name: answer-bot
description: LLM 终答 + 数值校验演示
system_prompt: 你是测试助手。
tools: [demo.shout]
max_steps: 3
llm: default
rules:
  - match: ["长度"]
    steps:
      - tool: demo.shout
        args: {text: "abc"}
"""


def _run_answer_bot(client, monkeypatch, tmp_path, answer_text: str) -> dict[str, Any]:
    """LLM 规划 1 步 demo.shout（length=3）→ 终答 answer_text → 返回 run 详情。"""
    from office_agent_runtime import loader
    from office_agent_runtime.planner.llm import LlmFunctionCallPlanner

    monkeypatch.setenv(
        "LLM_PROVIDERS",
        json.dumps(
            {"default": {"base_url": "http://llm.test/v1", "model": "fake", "api_key": "k"}}
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "tools" in body:  # 规划请求：提议 1 步工具调用
            call = {
                "id": "call_1",
                "type": "function",
                "function": {"name": "demo.shout", "arguments": json.dumps({"text": "abc"})},
            }
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "", "tool_calls": [call]}}
                    ]
                },
            )
        return httpx.Response(200, json=_chat_text(answer_text))  # 终答请求

    def _fake_client(_self, _cfg):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(LlmFunctionCallPlanner, "_ensure_client", _fake_client)
    monkeypatch.setattr(
        loader, "agents_dir", lambda: _yaml_dir(tmp_path, "answer-bot", _ANSWER_YAML)
    )

    token = _login(client)
    body = client.post(
        "/api/v1/runs", json={"agent": "answer-bot", "goal": "告诉我长度"}, headers=_auth(token)
    ).json()
    assert body["code"] == 0 and body["data"]["status"] == "DONE", body
    return client.get(f"/api/v1/runs/{body['data']['run_id']}", headers=_auth(token)).json()["data"]


def test_validation_pass_when_numbers_traceable(client, monkeypatch, tmp_path):
    """通过分支：终答数字 3 可在工具返回（length=3）中找到。"""
    data = _run_answer_bot(client, monkeypatch, tmp_path, "文本长度为 3 个字符")
    validation = data["validation"]
    assert validation["applicable"] is True and validation["passed"] is True
    assert validation["label"] == "已通过数值校验"
    assert validation["offenders"] == []
    assert data["answer"] == "文本长度为 3 个字符"
    assert any(item["value"] == 3 for item in validation["evidence"])


def test_validation_fail_marks_answer_untrustworthy(client, monkeypatch, tmp_path):
    """未通过分支：编造数字 999 被标失信；回答原样保留并附原始数据，不删除。"""
    data = _run_answer_bot(client, monkeypatch, tmp_path, "统计到 999 条记录")
    validation = data["validation"]
    assert validation["applicable"] is True and validation["passed"] is False
    assert validation["label"] == "未通过数值校验"
    assert [item["number"] for item in validation["offenders"]] == ["999"]
    assert data["answer"] == "统计到 999 条记录"  # 不删除回答，标注失信
    assert validation["evidence"]  # 附原始数据数值面


# ---------------- ④ 数值校验纯函数：日期字符串数字 + 边界 ----------------


def test_validate_answer_numbers_pure_function():
    obs = [{"index": 0, "tool": "demo", "result": {"count": 2, "date": "2026-09-22"}}]

    passed = validate_answer_numbers("共 2 项，日期 2026-09-22", obs)
    assert passed["passed"] is True and passed["offenders"] == []

    failed = validate_answer_numbers("共 3 项", obs)
    assert failed["passed"] is False
    assert [item["number"] for item in failed["offenders"]] == ["3"]

    # 无数字回答：全通过（无可校验对象）
    empty = validate_answer_numbers("没有数字", obs)
    assert empty["passed"] is True and empty["checked"] == []


# ---------------- ⑤ 规则智能体（无 LLM）：数值校验不适用 ----------------


def test_validation_not_applicable_for_rule_agent(client, agent_yaml_dir):
    token = _login(client)
    body = client.post(
        "/api/v1/runs", json={"agent": "demo-assistant", "goal": "请演示一下"}, headers=_auth(token)
    ).json()
    assert body["code"] == 0 and body["data"]["status"] == "DONE"
    data = client.get(f"/api/v1/runs/{body['data']['run_id']}", headers=_auth(token)).json()["data"]
    assert data["validation"]["applicable"] is False
    assert data["answer"] == ""
