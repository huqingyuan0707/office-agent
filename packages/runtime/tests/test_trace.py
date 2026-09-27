"""执行链路追踪单测（inner 解析 + 链路组装 + 管理门控，纯函数/零网络）

链路：trace.split_inner（终答内外分离）→ parse_inner_sections（三段切分与截断）
  → build_run_trace（意图/思考/路由/工具/汇总全段 + 零编造断言）
  → api._run_view（管理员透出 trace，普通用户无该键）。
对齐：PRD §4.3（交互体验）+ skills/doubao-coding-develop-unit-tests。
"""

from __future__ import annotations

import asyncio

from office_agent_runtime import api as runtime_api
from office_agent_runtime.trace import (
    TraceInput,
    build_run_trace,
    is_admin_roles,
    parse_inner_sections,
    split_inner,
)


def _results() -> dict:
    """单步 kb.ask 真实出参形状（latency_ms 实测耗时，绝非编造）。"""
    return {
        0: {
            "tool": "kb.ask",
            "status": "ok",
            "args": {"query": "费用报销的标准", "top_k": 3},
            "result": {"count": 2, "items": []},
            "latency_ms": 360,
        }
    }


def test_split_inner_separates_thought_and_public():
    inner, public = split_inner(
        "<<<inner>>>\n【Step 思考】想\n<<</inner>>>\n【对外回答】正在挑选智能体…"
    )
    assert "【Step 思考】" in inner and "正在挑选" not in inner
    assert public == "【对外回答】正在挑选智能体…"


def test_split_inner_without_marks_returns_full_public():
    inner, public = split_inner("纯模板直出正文")
    assert inner == "" and public == "纯模板直出正文"


def test_parse_inner_sections_splits_three_marks():
    sections = parse_inner_sections(
        "【Step 思考】分析需求\n【Step 路由】A 0.91\n【Step 工具调用】工具：X，入参{}"
    )
    assert [s["step_type"] for s in sections] == ["thought", "route", "tool_call"]
    assert all(s["content"] for s in sections)


def test_parse_inner_sections_empty_is_empty():
    assert parse_inner_sections("") == []
    assert parse_inner_sections("   ") == []


def test_build_run_trace_rule_path_has_no_fabricated_scores():
    trace = build_run_trace(
        TraceInput(
            run_id="r1",
            session_id="s1",
            goal="做一次经营数据分析",
            agent="office-assistant",
            planner_source="rule",
            route_note="规则关键词命中，直达确定性链",
            candidates=["office-assistant", "daily-report-assistant"],
            steps=_results(),
            public_answer="正在挑选智能体…",
            total_ms=720,
        )
    )
    assert trace["session_id"] == "s1" and trace["run_id"] == "r1"
    assert trace["total_ms"] == 720
    kinds = [s["step_type"] for s in trace["steps"]]
    assert kinds == ["intent", "thought", "route", "tool_call", "tool_result", "summary"]
    intent = trace["steps"][0]["content"]
    # 规则置信度如实注明非打分；候选只标选中/未选中，不编造 0.91/0.23 类小数
    assert "1.0（规则关键词命中，非模型打分）" in intent
    route = trace["steps"][2]["content"]
    assert "office-assistant✅（选中）" in route and "daily-report-assistant（未选中）" in route
    assert "0.91" not in route and "0.23" not in route
    call = trace["steps"][3]
    assert call["tool_name"] == "kb.ask" and call["time_cost_ms"] == 360
    assert "本次总耗时：720ms" in trace["steps"][-1]["content"]


def test_build_run_trace_llm_path_writes_no_score():
    trace = build_run_trace(
        TraceInput(goal="自由目标", agent="office-assistant", planner_source="react", steps={})
    )
    assert "（模型直定，无打分）" in trace["steps"][0]["content"]


def test_build_run_trace_uses_inner_sections_first():
    trace = build_run_trace(
        TraceInput(
            goal="g",
            agent="a",
            inner_sections=[{"step_type": "thought", "content": "模型亲笔", "truncated": False}],
        )
    )
    assert trace["steps"][1]["content"] == "模型亲笔"


def test_is_admin_roles_only_admin_and_star():
    assert is_admin_roles(["*"]) is True
    assert is_admin_roles(["admin"]) is True
    assert is_admin_roles(["office:read"]) is False
    assert is_admin_roles([]) is False


def test_run_view_gates_trace_by_role():
    """_run_view 管理门控：同一 checkpoint，admin 见 trace，普通用户无键。"""
    from office_agent_runtime.checkpoint import dumps

    trace = {"run_id": "r1", "steps": [], "total_ms": 1}

    def _task():
        from datetime import datetime

        from office_agent_server.models import Task

        row = Task(tenant="t", username="u", type="agent.run", status="DONE")
        row.created_at = datetime(2026, 9, 27, 12, 0, 0)
        row.checkpoint = dumps({"agent": "a", "goal": "g", "trace": trace})
        row.output = dumps({})
        return row

    admin_view = runtime_api._run_view(_task(), [], is_admin=True)
    user_view = runtime_api._run_view(_task(), [], is_admin=False)
    assert admin_view.get("trace") == trace
    assert "trace" not in user_view


def test_finalize_answer_splits_inner_from_public(monkeypatch):
    """终答 inner 剥离：对外 answer 不含块，inner 分段进链路（MockTransport 零网络）。"""
    import httpx

    from office_agent_runtime.planner.llm import LlmFunctionCallPlanner
    from office_agent_runtime.spec import parse_agent_spec
    from office_agent_runtime.validation import finalize_answer

    monkeypatch.setenv(
        "LLM_PROVIDERS",
        '{"default": {"base_url": "http://llm.test/v1", "model": "m", "api_key": "k"}}',
    )

    def _fake_client(_self, _cfg):
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "<<<inner>>>\n【Step 思考】看数据\n"
                                "<<</inner>>>\n结论正文"
                            }
                        }
                    ]
                },
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    monkeypatch.setattr(LlmFunctionCallPlanner, "_ensure_client", _fake_client)
    spec = parse_agent_spec(
        {
            "name": "answer-bot",
            "description": "终答 inner 剥离",
            "tools": ["demo.echo"],
            "max_steps": 1,
            "llm": "default",
        }
    )

    async def _run() -> dict:
        return await finalize_answer(spec=spec, goal="总结", results={})

    block = asyncio.run(_run())
    assert block["answer"] == "结论正文"
    assert block["inner"]["has_inner"] is True
    assert block["inner"]["sections"][0]["step_type"] == "thought"
