"""跨智能体路由单测（对话主入口：员工一句话 → 自动挑智能体）

链路：①route_agent_spec 纯函数三分支（规则命中 / LLM 兜底 / 全不中 1001 中文报错）
      ②POST /runs 省略 agent 的 HTTP 全链路（monkeypatch 假智能体清单，不出网）。

对齐：AGENTS.md §3（分层红线）；.trae/documents/智能体编排层实现方案.md §4
     （POST /runs 契约：agent 可选即自动路由）；skills/doubao-coding-develop-unit-tests/SKILL.md
"""

from __future__ import annotations

import pytest
from test_runtime import _auth, _login

from office_agent_core.errors import BusinessError
from office_agent_runtime import router as router_mod
from office_agent_runtime.router import route_agent_spec
from office_agent_runtime.spec import parse_agent_spec


def _rule_bot() -> object:
    """规则智能体：goal 含「日报」即命中。"""
    return parse_agent_spec(
        {
            "name": "rule-bot",
            "description": "规则智能体：出日报",
            "system_prompt": "你是测试助手。",
            "tools": ["demo.echo"],
            "max_steps": 2,
            "rules": [
                {"match": ["日报"], "steps": [{"tool": "demo.echo", "args": {"text": "hi"}}]}
            ],
        }
    )


def _llm_bot() -> object:
    """LLM 智能体：无 rules，llm profile 兜底路由的候选。"""
    return parse_agent_spec(
        {
            "name": "llm-bot",
            "description": "LLM 智能体：通用问答",
            "system_prompt": "你是测试助手。",
            "tools": ["demo.echo"],
            "max_steps": 2,
            "llm": "default",
        }
    )


def _hybrid_bot() -> object:
    """双配智能体：llm 主路径 + rules 降级保底（兜底优先级应高于纯 LLM 智能体）。"""
    return parse_agent_spec(
        {
            "name": "hybrid-bot",
            "description": "双配智能体：LLM 编排、规则保底",
            "system_prompt": "你是测试助手。",
            "tools": ["demo.echo"],
            "max_steps": 2,
            "llm": "default",
            "rules": [
                {"match": ["保底词"], "steps": [{"tool": "demo.echo", "args": {"text": "hi"}}]}
            ],
        }
    )


def _patch_specs(monkeypatch, specs: list) -> None:
    """替换路由模块的智能体清单来源（router 直接绑定了 load_agent_specs 名字）。"""
    monkeypatch.setattr(router_mod, "load_agent_specs", lambda: specs)


def _set_llm_profile(monkeypatch) -> None:
    """写入 LLM_PROVIDERS（profile 名 default，与 _llm_bot 的 llm 字段对齐）。"""
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        '{"default": {"base_url": "http://llm.test/v1", "model": "fake", "api_key": "k"}}',
    )


# ---------------- ① route_agent_spec 纯函数三分支 ----------------


def test_route_rule_hit_beats_llm(monkeypatch):
    """规则命中优先：即使清单里 LLM 智能体排前面，也选规则命中的那个。"""
    _patch_specs(monkeypatch, [_llm_bot(), _rule_bot()])
    assert route_agent_spec("请帮我出今天的日报").name == "rule-bot"


def test_route_llm_fallback_when_no_rule_hits(monkeypatch):
    """规则全不中且 LLM profile 已配置 → LLM 智能体兜底（执行期规划决定能不能干）。"""
    _patch_specs(monkeypatch, [_rule_bot(), _llm_bot()])
    _set_llm_profile(monkeypatch)
    assert route_agent_spec("帮我查个完全没规则覆盖的东西").name == "llm-bot"


def test_route_llm_fallback_prefers_hybrid_over_pure_llm(monkeypatch):
    """兜底优先级：rules+llm 双配的即使声明在后，也不让目录序靠前的纯 LLM 智能体抢单
    （防「报销」等办公目标被电商示例接走，回一句要订单编号的无效追问）。"""
    _patch_specs(monkeypatch, [_llm_bot(), _hybrid_bot()])
    _set_llm_profile(monkeypatch)
    assert route_agent_spec("帮我办一件两边规则都不沾的事").name == "hybrid-bot"


def test_route_llm_not_configured_means_no_fallback(monkeypatch):
    """规则全不中且 LLM profile 未配置 → 不兜底，如实 1001（不让用户白等一轮执行失败）。"""
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)
    _patch_specs(monkeypatch, [_rule_bot(), _llm_bot()])
    with pytest.raises(BusinessError) as excinfo:
        route_agent_spec("帮我查个完全没规则覆盖的东西")
    assert "rule-bot" in excinfo.value.msg


def test_route_no_match_raises_actionable(monkeypatch):
    """全不中 → 1001 中文可操作报错，报错里列出已装载智能体供用户换说法。

    注意清单里只有规则智能体：LLM 智能体在时会兜底接住任何目标，不会走全不中分支。
    """
    _patch_specs(monkeypatch, [_rule_bot()])
    with pytest.raises(BusinessError) as excinfo:
        route_agent_spec("完全不沾边的一句话")
    assert "rule-bot" in excinfo.value.msg


# ---------------- ② 纯寒暄秒回引导（不进规则、不进 LLM） ----------------


def test_route_greeting_only_instant_actionable(monkeypatch):
    """纯寒暄（你好/hello/在吗）：即使 LLM 已配置也不兜底，秒抛 1001 附可用说法。"""
    _patch_specs(monkeypatch, [_rule_bot(), _llm_bot()])
    _set_llm_profile(monkeypatch)
    for goal in ["你好", "你好！", "你好呀", "hello", "Hi", "在吗", "谢谢"]:
        with pytest.raises(BusinessError) as excinfo:
            route_agent_spec(goal)
        assert "一句话" in excinfo.value.msg
        assert "rule-bot" in excinfo.value.msg


def test_route_greeting_prefix_with_task_still_routes(monkeypatch):
    """问候 + 真事（你好，帮我出日报）：剥除问候后余下业务词，照常走规则路由。"""
    _patch_specs(monkeypatch, [_rule_bot(), _llm_bot()])
    assert route_agent_spec("你好，帮我出今天的日报").name == "rule-bot"


# ---------------- ③ POST /runs 省略 agent 的 HTTP 全链路 ----------------


def test_create_run_auto_routes_and_records_agent(client, monkeypatch):
    """POST /runs 不带 agent：路由到规则智能体并跑通，run 详情里 agent 记录为被路由者。"""
    _patch_specs(monkeypatch, [_rule_bot(), _llm_bot()])
    token = _login(client)
    resp = client.post(
        "/api/v1/runs",
        json={"goal": "请帮我出今天的日报"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    run_id = str(body["data"]["run_id"])

    detail = client.get(f"/api/v1/runs/{run_id}", headers=_auth(token)).json()
    assert detail["code"] == 0
    assert detail["data"]["agent"] == "rule-bot"
    assert detail["data"]["steps"], "规则智能体应至少执行一步 demo.echo"


def test_create_run_auto_route_no_match_1001(client, monkeypatch):
    """POST /runs 不带 agent 且规则智能体接不住（无 LLM 兜底者）：信封 1001，msg 可操作。"""
    _patch_specs(monkeypatch, [_rule_bot()])
    token = _login(client)
    resp = client.post(
        "/api/v1/runs",
        json={"goal": "完全不沾边的一句话"},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 1001
    assert "rule-bot" in body["msg"]


def test_create_run_auto_greeting_instant_done(client, monkeypatch):
    """POST /runs 不带 agent 且目标纯寒暄：200 即时 DONE run，answer 含使用引导。

    行为：零工具调用、零 LLM 出站（即使有 LLM 兜底者也不接），对话页走正常 run
    路径渲染终答；详情可查（DONE + 空步骤 + 同一 answer）。
    """
    _patch_specs(monkeypatch, [_rule_bot(), _llm_bot()])
    _set_llm_profile(monkeypatch)
    token = _login(client)
    resp = client.post(
        "/api/v1/runs",
        json={"goal": "你好"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["status"] == "DONE"
    assert "一句话" in str(data.get("answer") or "")
    run_id = str(data["run_id"])

    detail = client.get(f"/api/v1/runs/{run_id}", headers=_auth(token)).json()
    assert detail["code"] == 0
    assert detail["data"]["status"] == "DONE"
    assert detail["data"]["steps"] == []
    assert "一句话" in str(detail["data"].get("answer") or "")


def test_create_run_explicit_agent_greeting_not_intercepted(client, monkeypatch):
    """显式指定智能体 + 寒暄目标：不拦截，照常跑所选智能体（规则不中则 FAILED 留痕）。

    注意受理层显式路径走 ``loader.find_agent_spec``（真插件目录），与自动路由的
    ``router.load_agent_specs`` 打包补丁不是同一绑定，这里一并把前者指到假清单。
    """
    from office_agent_runtime import loader as loader_mod

    specs = [_rule_bot(), _llm_bot()]
    _patch_specs(monkeypatch, specs)
    by_name = {spec.name: spec for spec in specs}
    monkeypatch.setattr(loader_mod, "find_agent_spec", lambda name: by_name[name])
    token = _login(client)
    resp = client.post(
        "/api/v1/runs",
        json={"agent": "rule-bot", "goal": "你好"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["agent"] == "rule-bot"
    assert body["data"]["status"] == "FAILED"
