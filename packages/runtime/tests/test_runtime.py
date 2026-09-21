"""runtime 单测：规划器 / 配置校验与装载 / API 全链路 / 白名单拒 / 步数顶 / 断点续跑

口径：全程不出网，工具用本地 echo 类 handler；智能体配置经 tmp_path + monkeypatch
提供，不写仓库 plugins/。
"""

from __future__ import annotations

import pytest

from office_agent_core.errors import BusinessError
from office_agent_runtime import loader
from office_agent_runtime.planner.rule import RulePlanner
from office_agent_runtime.spec import PlannerStep, parse_agent_spec


def _login(client) -> str:
    """登录取 token（种子账号由 lifespan 建好）。"""
    resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    return str(body["data"]["token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------- ① RulePlanner：命中 / 未命中 / 取值模板 / 缺值报错 ----------------


def _planner_spec() -> object:
    return parse_agent_spec(
        {
            "name": "p1",
            "tools": ["demo.echo", "demo.shout"],
            "max_steps": 3,
            "rules": [
                {
                    "match": ["演示"],
                    "steps": [
                        {"tool": "demo.echo", "args": {"text": "hello"}},
                        {
                            "tool": "demo.shout",
                            "args": {
                                "text": "{steps[0].result.echo.text}",
                                "note": "值={steps[0].result.echo.text}!",
                                "items": "{steps[0].result.list}",
                            },
                        },
                    ],
                }
            ],
        }
    )


def _history_results() -> dict[int, dict]:
    """模拟 executor.call 出参历史（步号 → 完整出参，取值模板从 result 里取）。"""
    return {0: {"result": {"echo": {"text": "hello"}, "list": [1, 2]}}}


def test_rule_planner_hit_miss_and_templates():
    planner = RulePlanner(_planner_spec())  # type: ignore[arg-type]
    hit = planner.plan("请演示一下")
    assert hit and hit[0].tool == "demo.echo"
    assert hit[0].args == {"text": "hello"}

    resolved = RulePlanner.resolve_args(hit[1].args, _history_results())
    assert resolved["text"] == "hello"  # 整串模板：原类型透传
    assert resolved["note"] == "值=hello!"  # 嵌入模板：文本替换
    assert resolved["items"] == [1, 2]  # 列表结果原样取出

    assert planner.plan("无关目标") == []  # 未命中

    # 模板是副本：调用方改动不污染规则声明
    hit[0].args["text"] = "改过"
    assert _planner_spec().rules[0].steps[0].args["text"] == "hello"  # type: ignore[union-attr,index]


def test_rule_planner_missing_value_raises():
    with pytest.raises(BusinessError) as excinfo:
        RulePlanner.resolve_args({"text": "{steps[3].result.x}"}, _history_results())
    assert "步骤 3" in excinfo.value.msg

    with pytest.raises(BusinessError) as excinfo:
        RulePlanner.resolve_args({"text": "{steps[0].result.nope}"}, _history_results())
    assert "找不到字段" in excinfo.value.msg


# ---------------- ② spec 校验拒绝非法配置 + loader 容错 ----------------


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"tools": ["demo.echo"]}, "name"),
        ({"name": "x"}, "tools"),
        ({"name": "x", "tools": ["demo.echo"], "max_steps": 0}, "max_steps"),
        (
            {
                "name": "x",
                "tools": ["demo.echo"],
                "rules": [{"match": ["a"], "steps": [{"tool": "demo.other", "args": {}}]}],
            },
            "白名单",
        ),
    ],
)
def test_parse_agent_spec_rejects_invalid(raw: dict, fragment: str):
    with pytest.raises(BusinessError) as excinfo:
        parse_agent_spec(raw)
    assert fragment in excinfo.value.msg


def test_loader_skips_invalid_and_missing_dir(tmp_path, monkeypatch):
    # 目录不存在 → 空列表，绝不抛错
    monkeypatch.setattr(loader, "agents_dir", lambda: tmp_path / "absent")
    assert loader.load_agent_specs() == []

    # 单文件坏（缺 name / 非法 YAML）→ 告警跳过，不拖垮进程
    root = tmp_path / "plugins"
    for dirname, text in (("bad-agent", "tools: [x]\n"), ("broken", "a: [1, 2")):
        target = root / dirname
        target.mkdir(parents=True)
        (target / "agent.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(loader, "agents_dir", lambda: root)
    assert loader.load_agent_specs() == []


# ---------------- ③ API 全链路 ----------------


def test_agents_endpoint_lists_loaded_specs(client, agent_yaml_dir):
    token = _login(client)
    body = client.get("/api/v1/agents", headers=_auth(token)).json()
    assert body["code"] == 0
    names = {item["name"] for item in body["data"]["items"]}
    assert {"demo-assistant", "capped-agent"} <= names
    demo = next(item for item in body["data"]["items"] if item["name"] == "demo-assistant")
    assert demo["max_steps"] == 4
    assert "demo.echo" in demo["tools"]


def test_post_run_executes_two_steps_end_to_end(client, agent_yaml_dir):
    token = _login(client)
    resp = client.post(
        "/api/v1/runs",
        json={"agent": "demo-assistant", "goal": "请演示一下"},
        headers={**_auth(token), "X-Trace-Id": "tr-run-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0  # 受理即返回概要
    assert body["trace_id"] == "tr-run-1"
    run_id = body["data"]["run_id"]
    assert body["data"]["status"] == "DONE"

    # 状态 + 时间线：Task.checkpoint 已推进到末尾，RunStep 两行且 trace 同一条线
    detail = client.get(f"/api/v1/runs/{run_id}", headers=_auth(token)).json()
    assert detail["code"] == 0
    data = detail["data"]
    assert data["status"] == "DONE" and data["error"] == ""
    assert data["next_step"] == 2  # Task.checkpoint 已更新
    assert [step["step_index"] for step in data["steps"]] == [0, 1]
    step0, step1 = data["steps"]
    assert step0["status"] == "ok" and step0["trace_id"] == "tr-run-1"
    assert step0["args"] == {"text": "hello"}
    assert step0["result"]["echo"] == {"text": "hello"}
    assert step1["args"] == {"text": "hello"}  # 取值模板生效
    assert step1["result"]["shout"] == "HELLO"
    assert step1["trace_id"] == "tr-run-1"

    # Run 复用 Task 表：任务列表可见 type=agent.run 的行
    tasks = client.get("/api/v1/tasks", headers=_auth(token)).json()["data"]
    run_task = next(item for item in tasks if item["id"] == run_id)
    assert run_task["type"] == "agent.run" and run_task["status"] == "DONE"


def test_post_run_unknown_agent_is_404(client, agent_yaml_dir):
    token = _login(client)
    resp = client.post("/api/v1/runs", json={"agent": "nope", "goal": "x"}, headers=_auth(token))
    assert resp.status_code == 404
    assert resp.json()["code"] == 1004

    resp = client.get("/api/v1/runs/nope", headers=_auth(token))
    assert resp.status_code == 404
    assert resp.json()["code"] == 1004


def test_post_run_goal_misses_rules_fails_run(client, agent_yaml_dir):
    """未命中规则：受理仍 code 0，run 收敛 FAILED（受理与执行分离）。"""
    token = _login(client)
    body = client.post(
        "/api/v1/runs", json={"agent": "demo-assistant", "goal": "无关目标"}, headers=_auth(token)
    ).json()
    assert body["code"] == 0
    assert body["data"]["status"] == "FAILED"
    assert "未命中" in body["data"]["error"]
    data = client.get(f"/api/v1/runs/{body['data']['run_id']}", headers=_auth(token)).json()["data"]
    assert data["steps"] == []  # 未产生步骤执行，无幻影行


# ---------------- ④ 白名单外工具 → 该步 failed + 中文原因 ----------------


def test_whitelist_out_tool_fails_step_with_chinese_reason(client, agent_yaml_dir, monkeypatch):
    """模拟规划器越权提议：白名单是运行时最后防线，拒绝理由落在时间线里。"""
    from office_agent_core import registry
    from office_agent_core.contracts import ToolContext, ToolSpec
    from office_agent_runtime import runner

    async def _rogue(_ctx: ToolContext, _args: dict) -> dict:
        return {"rogue": True}

    # 先注册成可用工具：拦下它的只能是白名单，而不是注册中心
    registry.register(
        ToolSpec(
            name="demo.rogue",
            scope="office:read",
            description="白名单外的可用工具",
            params={},
            handler=_rogue,
        )
    )

    async def _rogue_plan(_self, _goal):
        return [PlannerStep(tool="demo.rogue", args={})]

    monkeypatch.setattr(runner.RulePlanner, "plan", _rogue_plan)
    token = _login(client)
    body = client.post(
        "/api/v1/runs", json={"agent": "demo-assistant", "goal": "演示"}, headers=_auth(token)
    ).json()
    assert body["code"] == 0  # 受理成功，执行失败在时间线
    run_id = body["data"]["run_id"]
    assert body["data"]["status"] == "FAILED"

    data = client.get(f"/api/v1/runs/{run_id}", headers=_auth(token)).json()["data"]
    assert data["status"] == "FAILED"
    assert len(data["steps"]) == 1
    assert data["steps"][0]["status"] == "failed"
    assert "白名单" in data["steps"][0]["result"]
    assert "白名单" in data["error"]


# ---------------- ⑤ 超 max_steps → 终态 ----------------


def test_max_steps_cap_ends_run_in_terminal_state(client, agent_yaml_dir):
    token = _login(client)
    body = client.post(
        "/api/v1/runs", json={"agent": "capped-agent", "goal": "连跑一下"}, headers=_auth(token)
    ).json()
    assert body["code"] == 0
    run_id = body["data"]["run_id"]
    assert body["data"]["status"] == "FAILED"

    data = client.get(f"/api/v1/runs/{run_id}", headers=_auth(token)).json()["data"]
    assert data["status"] == "FAILED"
    assert [step["step_index"] for step in data["steps"]] == [0]  # 只有第 1 步真的执行了
    assert data["steps"][0]["status"] == "ok"
    assert "最大步数" in data["error"]


# ---------------- ⑥ 构造中断 checkpoint → resume 续跑成功 ----------------


def test_resume_continues_from_checkpoint(client, agent_yaml_dir):
    from office_agent_core import registry
    from office_agent_core.contracts import ToolContext, ToolSpec

    token = _login(client)
    # 第一次：第二步恒失败 → run FAILED，checkpoint 停在第 2 步（next_step=1）
    body = client.post(
        "/api/v1/runs", json={"agent": "demo-assistant", "goal": "请演示一下"}, headers=_auth(token)
    ).json()
    run_id = body["data"]["run_id"]
    assert body["data"]["status"] == "FAILED"
    failed = client.get(f"/api/v1/runs/{run_id}", headers=_auth(token)).json()["data"]
    assert [step["status"] for step in failed["steps"]] == ["ok", "failed"]
    assert failed["next_step"] == 1

    # 修复故障（换上健康实现）后显式续跑：从断点继续并收敛 DONE
    async def _healed(_ctx: ToolContext, _args: dict) -> dict:
        return {"shout": "WORLD", "length": 5}

    registry.register(
        ToolSpec(
            name="demo.shout",
            scope="office:read",
            description="已修复",
            params={
                "type": "object",
                "properties": {"text": {"type": "string", "title": "内容"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=_healed,
        )
    )
    resumed = client.post(f"/api/v1/runs/{run_id}/resume", headers=_auth(token)).json()
    assert resumed["code"] == 0
    assert resumed["data"]["status"] == "DONE"
    assert resumed["data"]["error"] == ""

    data = client.get(f"/api/v1/runs/{run_id}", headers=_auth(token)).json()["data"]
    assert data["status"] == "DONE" and data["error"] == ""
    # 失败尝试留痕 + 重试成功：同 step_index 两行，按时间排列
    assert [step["step_index"] for step in data["steps"]] == [0, 1, 1]
    assert data["steps"][1]["status"] == "failed"
    assert data["steps"][2]["status"] == "ok"
    # 续跑步骤的取值模板从 checkpoint 里的历史结果取值成功
    assert data["steps"][2]["args"] == {"text": "hello"}
    assert data["steps"][2]["result"]["shout"] == "WORLD"


def test_resume_done_run_is_rejected(client, agent_yaml_dir):
    """DONE 是唯一终态（内核口径）：已完成拒绝续跑（4009）。"""
    token = _login(client)
    body = client.post(
        "/api/v1/runs", json={"agent": "demo-assistant", "goal": "请演示一下"}, headers=_auth(token)
    ).json()
    run_id = body["data"]["run_id"]
    resp = client.post(f"/api/v1/runs/{run_id}/resume", headers=_auth(token))
    assert resp.status_code == 400
    assert resp.json()["code"] == 4009
