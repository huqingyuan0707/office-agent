"""LLM 函数调用规划器单测（R1：MockTransport 假 LLM，全程不出网）

链路：LLM_PROVIDERS 环境变量提供 profile → LlmFunctionCallPlanner 注入
      httpx.MockTransport 假客户端 → plan() 解析 tool_calls 出提议；
      Runner 降级链经 HTTP 全链路验证：正常 tool_calls 记 planner_source="llm"，
      上游 401 降级 RulePlanner 记 "rule"，profile 未配置且无 rules 收敛 FAILED
      中文可操作错误（受理与执行分离，绝不编造规划）。

对齐：.trae/documents/智能体编排层实现方案.md §R1；skills/doubao-coding-develop-unit-tests/SKILL.md
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from test_runtime import _auth, _login

from office_agent_runtime.planner.llm import LlmFunctionCallPlanner
from office_agent_runtime.spec import parse_agent_spec

#: 假 LLM 的 base_url（MockTransport 拦截，零真实网络）
_LLM_BASE = "http://llm.test/v1"


def _set_providers(monkeypatch, *, api_key: str = "sk-test") -> None:
    """写入 LLM_PROVIDERS 环境变量（profile 名 default，与 agent.yaml 的 llm 字段对齐）。"""
    payload = {"default": {"base_url": _LLM_BASE, "model": "fake-model", "api_key": api_key}}
    monkeypatch.setenv("LLM_PROVIDERS", json.dumps(payload))


def _mount_fake_llm(monkeypatch, handler) -> None:
    """把规划器的出站客户端换成 MockTransport 假 LLM（真实网络零触达）。"""

    def _fake_client(_self, _cfg):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(LlmFunctionCallPlanner, "_ensure_client", _fake_client)


def _tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """OpenAI tool_calls 单项（协议约定 arguments 是 JSON 字符串）。"""
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _chat_response(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """chat.completions 假响应体。"""
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": calls}}]}


def _llm_spec():
    """带 llm profile 的智能体声明（工具用 conftest 注册的本地 echo 类）。"""
    return parse_agent_spec(
        {
            "name": "llm-bot",
            "description": "LLM 规划单测",
            "system_prompt": "你是测试助手。",
            "tools": ["demo.echo", "demo.shout"],
            "max_steps": 3,
            "llm": "default",
        }
    )


_LLM_YAML = """\
name: llm-assistant
description: LLM 规划演示：正常记 llm 来源，401 降级规则记 rule
system_prompt: 你是测试助手。
tools: [demo.echo]
max_steps: 3
llm: default
rules:
  - match: ["演示"]
    steps:
      - tool: demo.echo
        args: {text: "rule-hello"}
"""

_LLM_ONLY_YAML = """\
name: llm-only
description: 只配 LLM 不配规则：LLM 不可用必须中文报错
tools: [demo.echo]
max_steps: 3
llm: default
"""


def _yaml_dir(tmp_path, dirname: str, text: str):
    """写一份 agent.yaml 到临时 plugins 目录（不污染仓库 plugins/）。"""
    root = tmp_path / "plugins"
    target = root / dirname
    target.mkdir(parents=True)
    (target / "agent.yaml").write_text(text, encoding="utf-8")
    return root


# ---------------- ① 正常 tool_calls：单轮多提议 + 出站请求体合规 ----------------


def test_llm_planner_parses_tool_calls(monkeypatch):
    _set_providers(monkeypatch)
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization", "")
        seen["model"] = body["model"]
        seen["tools"] = [item["function"]["name"] for item in body["tools"]]
        seen["first_role"] = body["messages"][0]["role"]
        return httpx.Response(
            200,
            json=_chat_response(
                [
                    _tool_call("demo.echo", {"text": "hello"}),
                    _tool_call("demo.shout", {"text": "{steps[0].result.echo.text}"}),
                ]
            ),
        )

    _mount_fake_llm(monkeypatch, handler)

    async def _plan():
        planner = LlmFunctionCallPlanner.from_spec(_llm_spec())
        try:
            return await planner.plan("请演示")
        finally:
            await planner.aclose()

    steps = asyncio.run(_plan())
    assert [step.tool for step in steps] == ["demo.echo", "demo.shout"]
    assert steps[0].args == {"text": "hello"}
    # 出站请求合规：endpoint / 模型 / 凭据 / tools=白名单交集 / system prompt 注入
    assert seen["url"] == f"{_LLM_BASE}/chat/completions"
    assert seen["model"] == "fake-model"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["tools"] == ["demo.echo", "demo.shout"]
    assert seen["first_role"] == "system"


# ---------------- ② LLM 正常 → 整条 run 走 LLM 提议，来源记 llm ----------------


def test_llm_plan_run_marks_planner_source_llm(client, monkeypatch, tmp_path):
    from office_agent_runtime import loader

    monkeypatch.setattr(
        loader, "agents_dir", lambda: _yaml_dir(tmp_path, "llm-assistant", _LLM_YAML)
    )
    _set_providers(monkeypatch)
    _mount_fake_llm(
        monkeypatch,
        lambda request: httpx.Response(
            200, json=_chat_response([_tool_call("demo.echo", {"text": "llm-hello"})])
        ),
    )

    token = _login(client)
    body = client.post(
        "/api/v1/runs",
        json={"agent": "llm-assistant", "goal": "请演示一下"},
        headers=_auth(token),
    ).json()
    assert body["code"] == 0
    assert body["data"]["status"] == "DONE"
    data = client.get(f"/api/v1/runs/{body['data']['run_id']}", headers=_auth(token)).json()["data"]
    assert data["steps"][0]["planner_source"] == "llm"  # 可观测：这步是 LLM 定的
    assert data["steps"][0]["tool"] == "demo.echo"
    assert data["steps"][0]["args"] == {"text": "llm-hello"}


# ---------------- ③ 上游 401 → 降级 RulePlanner，来源记 rule ----------------


def test_llm_401_degrades_to_rule_planner(client, monkeypatch, tmp_path):
    from office_agent_runtime import loader

    monkeypatch.setattr(
        loader, "agents_dir", lambda: _yaml_dir(tmp_path, "llm-assistant", _LLM_YAML)
    )
    _set_providers(monkeypatch)
    _mount_fake_llm(monkeypatch, lambda request: httpx.Response(401, json={"error": "bad key"}))

    token = _login(client)
    body = client.post(
        "/api/v1/runs",
        json={"agent": "llm-assistant", "goal": "请演示一下"},
        headers=_auth(token),
    ).json()
    assert body["code"] == 0
    assert body["data"]["status"] == "DONE"  # 规则规划接棒，运行照常收敛
    data = client.get(f"/api/v1/runs/{body['data']['run_id']}", headers=_auth(token)).json()["data"]
    assert data["steps"][0]["planner_source"] == "rule"
    assert data["steps"][0]["args"] == {"text": "rule-hello"}  # 走的是 rules 模板而非 LLM 提议


# ---------------- ④ profile 未配置且无 rules → 中文可操作错误 ----------------


def test_llm_unconfigured_without_rules_fails_actionable(client, monkeypatch, tmp_path):
    from office_agent_runtime import loader

    monkeypatch.setattr(
        loader, "agents_dir", lambda: _yaml_dir(tmp_path, "llm-only", _LLM_ONLY_YAML)
    )
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)

    token = _login(client)
    body = client.post(
        "/api/v1/runs",
        json={"agent": "llm-only", "goal": "随便跑跑"},
        headers=_auth(token),
    ).json()
    assert body["code"] == 0  # 受理与执行分离：受理仍 code 0
    assert body["data"]["status"] == "FAILED"
    error = body["data"]["error"]
    assert "LLM 不可用" in error and "未配置 rules" in error
    data = client.get(f"/api/v1/runs/{body['data']['run_id']}", headers=_auth(token)).json()["data"]
    assert data["steps"] == []  # 无规划就不执行：零幻影步骤
