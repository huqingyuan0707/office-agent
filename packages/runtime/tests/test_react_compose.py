"""ReAct 逐步再规划 + {steps[*].result} 占位符 + llm_mode 单测（MockTransport 假 LLM 全程不出网）

链路：占位符整体注入（RulePlanner.resolve_args）→ spec.llm_mode 校验 →
      ReACTPlanner.next_step（tool_calls 出一步 / 纯文本收工 / 观察截断）→
      HTTP 全链路 react run（规则快路径优先、自由目标逐步再规划、中途失败带产出
      收敛、零产出失败、profile 未配置受理即报错）。

对齐：.trae/documents/对话办理重构-大模型串联三场景.md §机制件 1/2/4；
      skills/doubao-coding-develop-unit-tests/SKILL.md。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from test_planner_llm import (
    _chat_response,
    _mount_fake_llm,
    _set_providers,
    _tool_call,
    _yaml_dir,
)
from test_runtime import _auth, _login

from office_agent_core.errors import BusinessError
from office_agent_runtime.planner.react import ReACTPlanner
from office_agent_runtime.planner.rule import RulePlanner
from office_agent_runtime.spec import PlannerStep, parse_agent_spec

# ---------------- ① {steps[*].result}：整体注入 / 空历史报错 / 拒绝字段路径 ----------------


def test_all_steps_placeholder_resolves_to_observation_list():
    results = {
        0: {"tool": "demo.echo", "status": "ok", "result": {"echo": {"text": "hi"}}},
        1: {"tool": "demo.shout", "status": "ok", "result": {"shout": "HI"}},
    }
    resolved = RulePlanner.resolve_args({"material": "{steps[*].result}"}, results)
    assert resolved["material"] == [
        {"index": 0, "tool": "demo.echo", "result": {"echo": {"text": "hi"}}},
        {"index": 1, "tool": "demo.shout", "result": {"shout": "HI"}},
    ]


def test_all_steps_placeholder_requires_results():
    with pytest.raises(BusinessError) as excinfo:
        RulePlanner.resolve_args({"material": "{steps[*].result}"}, {})
    assert "还没有任何步骤出参" in excinfo.value.msg


def test_all_steps_placeholder_rejects_field_path():
    with pytest.raises(BusinessError) as excinfo:
        RulePlanner.resolve_args({"m": "{steps[*].result.x}"}, {0: {"result": {}}})
    assert "不接受字段路径" in excinfo.value.msg


# ---------------- ② llm_mode：默认 plan / react 合法 / 非法值中文报错 ----------------


def test_llm_mode_defaults_to_plan_and_validates():
    base = {"name": "x", "tools": ["demo.echo"], "max_steps": 1}
    assert parse_agent_spec(dict(base)).llm_mode == "plan"  # 缺省零回归
    assert parse_agent_spec({**base, "llm_mode": "react"}).llm_mode == "react"
    with pytest.raises(BusinessError) as excinfo:
        parse_agent_spec({**base, "llm_mode": "fast"})
    assert "llm_mode" in excinfo.value.msg and "react" in excinfo.value.msg


# ---------------- ③ ReACTPlanner.next_step：单步提议 / 纯文本收工 / 观察截断 ----------------


def _react_spec():
    return parse_agent_spec(
        {
            "name": "react-bot",
            "description": "ReAct 单测",
            "system_prompt": "你是测试助手。",
            "tools": ["demo.echo", "demo.shout"],
            "max_steps": 4,
            "llm": "default",
            "llm_mode": "react",
        }
    )


def _history() -> dict[int, dict[str, Any]]:
    return {0: {"tool": "demo.echo", "status": "ok", "result": {"echo": {"text": "hi"}}}}


def test_react_next_step_parses_single_tool_call(monkeypatch):
    _set_providers(monkeypatch)
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["user"] = body["messages"][-1]["content"]
        seen["tools"] = [item["function"]["name"] for item in body["tools"]]
        return httpx.Response(
            200, json=_chat_response([_tool_call("demo.echo", {"text": "step-1"})])
        )

    _mount_fake_llm(monkeypatch, handler)

    async def _run():
        planner = ReACTPlanner.from_spec(_react_spec())
        try:
            return await planner.next_step("查一下销量", _history())
        finally:
            await planner.aclose()

    step = asyncio.run(_run())
    assert step == PlannerStep(tool="demo.echo", args={"text": "step-1"})
    # 出站请求带目标 + 真实出参观察（工具名/状态）+ 白名单 tools
    assert "查一下销量" in seen["user"]
    assert "demo.echo" in seen["user"] and '"ok"' in seen["user"]
    assert seen["tools"] == ["demo.echo", "demo.shout"]


def test_react_next_step_plain_text_means_done(monkeypatch):
    _set_providers(monkeypatch)
    _mount_fake_llm(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "完成"}}]}
        ),
    )

    async def _run():
        planner = ReACTPlanner.from_spec(_react_spec())
        try:
            return await planner.next_step("查一下销量", _history())
        finally:
            await planner.aclose()

    assert asyncio.run(_run()) is None


def test_react_next_step_truncates_huge_results(monkeypatch):
    _set_providers(monkeypatch)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["user"] = body["messages"][-1]["content"]
        return httpx.Response(200, json=_chat_response([_tool_call("demo.echo", {})]))

    _mount_fake_llm(monkeypatch, handler)
    results = {0: {"tool": "demo.echo", "status": "ok", "result": {"data": "x" * 2000}}}

    async def _run():
        planner = ReACTPlanner.from_spec(_react_spec())
        try:
            return await planner.next_step("查一下销量", results)
        finally:
            await planner.aclose()

    assert asyncio.run(_run()) is not None
    assert "已截断" in seen["user"]  # 超长出参截断带标记，绝不伪装完整


# ---------------- ④ HTTP 全链路：react 智能体的混合模式 ----------------

_REACT_YAML = """\
name: react-assistant
description: ReAct 演示：规则快路径 + 自由目标逐步再规划
system_prompt: 你是测试助手。
tools: [demo.echo, demo.shout]
max_steps: 4
llm: default
llm_mode: react
rules:
  - match: ["演示"]
    steps:
      - tool: demo.echo
        args: {text: "rule-hello"}
"""


def _run_goal(client, token: str, goal: str) -> dict:
    body = client.post(
        "/api/v1/runs",
        json={"agent": "react-assistant", "goal": goal},
        headers=_auth(token),
    ).json()
    assert body["code"] == 0
    return client.get(f"/api/v1/runs/{body['data']['run_id']}", headers=_auth(token)).json()["data"]


def test_react_rule_fast_path_beats_llm(client, monkeypatch, tmp_path):
    """规则命中走快路径：LLM 挂了（401）也不该被调用，来源记 rule。"""
    from office_agent_runtime import loader

    monkeypatch.setattr(
        loader, "agents_dir", lambda: _yaml_dir(tmp_path, "react-assistant", _REACT_YAML)
    )
    _set_providers(monkeypatch)
    _mount_fake_llm(monkeypatch, lambda request: httpx.Response(401, json={"error": "bad key"}))

    data = _run_goal(client, _login(client), "请演示一下")
    assert data["status"] == "DONE"
    assert data["steps"][0]["planner_source"] == "rule"
    assert data["steps"][0]["args"] == {"text": "rule-hello"}


def test_react_free_goal_steps_then_concludes(client, monkeypatch, tmp_path):
    """自由目标（规则不中）：LLM 提议一步 → 执行 → LLM 判定收工 → DONE。"""
    from office_agent_runtime import loader

    monkeypatch.setattr(
        loader, "agents_dir", lambda: _yaml_dir(tmp_path, "react-assistant", _REACT_YAML)
    )
    _set_providers(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json=_chat_response([_tool_call("demo.echo", {"text": "react-1"})])
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "完成"}}]}
        )

    _mount_fake_llm(monkeypatch, handler)

    data = _run_goal(client, _login(client), "自由发挥一下")
    assert data["status"] == "DONE"
    assert data["steps"][0]["planner_source"] == "react"
    assert data["steps"][0]["args"] == {"text": "react-1"}
    assert len(data["steps"]) == 1  # LLM 第二轮判定收工，不追加幻影步骤
    # 三轮出站 = react 提议一轮 + react 收工判定一轮 + 终答合成一轮（finalize_answer）
    assert calls["n"] == 3


def test_react_midway_failure_concludes_with_partial_results(client, monkeypatch, tmp_path):
    """中途规划失败：已有真实产出则带产出收敛 DONE（不丢弃已执行步骤）。"""
    from office_agent_runtime import loader

    monkeypatch.setattr(
        loader, "agents_dir", lambda: _yaml_dir(tmp_path, "react-assistant", _REACT_YAML)
    )
    _set_providers(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json=_chat_response([_tool_call("demo.echo", {"text": "react-1"})])
            )
        return httpx.Response(401, json={"error": "bad key"})

    _mount_fake_llm(monkeypatch, handler)

    data = _run_goal(client, _login(client), "自由发挥一下")
    assert data["status"] == "DONE"  # 零产出才 FAILED；有产出带产出收敛
    assert len(data["steps"]) == 1


def test_react_zero_output_failure_is_actionable(client, monkeypatch, tmp_path):
    """零产出失败：LLM 全程不可用且规则不中 → FAILED 中文可操作错误。"""
    from office_agent_runtime import loader

    monkeypatch.setattr(
        loader, "agents_dir", lambda: _yaml_dir(tmp_path, "react-assistant", _REACT_YAML)
    )
    _set_providers(monkeypatch)
    _mount_fake_llm(monkeypatch, lambda request: httpx.Response(401, json={"error": "bad key"}))

    data = _run_goal(client, _login(client), "自由发挥一下")
    assert data["status"] == "FAILED"
    assert "ReAct 规划不可用" in data["error"]
    assert data["steps"] == []


def test_react_profile_unconfigured_fails_at_plan_stage(client, monkeypatch, tmp_path):
    """profile 未配置：受理 code 0、执行即 FAILED（受理与执行分离），不浪费出站。"""
    from office_agent_runtime import loader

    monkeypatch.setattr(
        loader, "agents_dir", lambda: _yaml_dir(tmp_path, "react-assistant", _REACT_YAML)
    )
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)

    data = _run_goal(client, _login(client), "自由发挥一下")
    assert data["status"] == "FAILED"
    assert "未在 LLM_PROVIDERS 配置" in data["error"]
    assert data["steps"] == []
