"""Agent 评测 harness 单测（数据集校验 / scorer 链 / 端到端小样本）

链路：①load_dataset 正常与坏数据路径（中文报错、空集、坏枚举）；
      ②四个 scorer 纯函数路径（exact/subsequence/数值溯源/关键词/skipped 语义）；
      ③run_eval 端到端（自备 tmp plugins + conftest 的 demo 演示工具，零外部依赖）。

对齐：eval/ 子包各模块 docstring；AGENTS.md §3（分层红线）；
      skills/doubao-coding-develop-unit-tests/SKILL.md。
"""

from __future__ import annotations

import pytest

from office_agent_core.errors import BusinessError
from office_agent_runtime.eval.dataset import EvalCase, load_dataset
from office_agent_runtime.eval.harness import Trajectory, run_eval
from office_agent_runtime.eval.report import render_json, render_markdown
from office_agent_runtime.eval.scorers import (
    AnswerJudgeScorer,
    AnswerKeywordScorer,
    NumberGroundingScorer,
    ToolSelectionScorer,
)

#: 端到端用的演示 plugins（复用 conftest autouse 注册的 demo.echo/demo.shout）
_EVAL_PLUGIN_YAML = """\
name: eval-demo
description: 评测单测演示智能体
system_prompt: |
  你是评测演示助手。
tools: [demo.echo]
max_steps: 2
rules:
  - match: ["回声"]
    steps:
      - tool: demo.echo
        args: {text: "hello"}
"""


def _write_dataset(tmp_path, text: str):
    """把数据集 YAML 写临时文件并返回路径。"""
    path = tmp_path / "dataset.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _write_plugins(tmp_path) -> object:
    """写演示 plugins 目录并返回路径（loader 由 harness 临时指向）。"""
    root = tmp_path / "plugins"
    target = root / "eval-demo"
    target.mkdir(parents=True)
    (target / "agent.yaml").write_text(_EVAL_PLUGIN_YAML, encoding="utf-8")
    return root


# ---- ① 数据集加载与校验 ----


def test_load_dataset_happy_path(tmp_path):
    """正常数据集：字段解析 + 去重保序 + tool_match 默认 exact。"""
    path = _write_dataset(
        tmp_path,
        """
cases:
  - id: c1
    agent: a
    goal: g
    expect_tools: [demo.echo, demo.echo, demo.shout]
  - id: c2
    agent: a
    goal: g
    tool_match: subsequence
    expect_numbers: ["3"]
    answer_contains: ["ok", "ok"]
""",
    )
    cases = load_dataset(path)
    assert [case.id for case in cases] == ["c1", "c2"]
    assert cases[0].expect_tools == ("demo.echo", "demo.shout")
    assert cases[0].tool_match == "exact"
    assert cases[1].tool_match == "subsequence"
    assert cases[1].expect_numbers == ("3",)
    assert cases[1].answer_contains == ("ok",)


def test_load_dataset_empty_cases_returns_empty(tmp_path):
    """空 cases / 空文件都给空列表（调用方按空集处理，不炸）。"""
    assert load_dataset(_write_dataset(tmp_path, "cases: []")) == []
    assert load_dataset(_write_dataset(tmp_path, "")) == []


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("cases: {a: 1}", "cases 必须是列表"),
        (
            """
cases:
  - agent: a
    goal: g
""",
            "缺少 id",
        ),
        (
            """
cases:
  - id: c
    goal: g
""",
            "缺少 agent",
        ),
        (
            """
cases:
  - id: c
    agent: a
""",
            "缺少 goal",
        ),
        (
            """
cases:
  - id: c
    agent: a
    goal: g
    tool_match: fuzzy
""",
            "tool_match",
        ),
        (
            """
cases:
  - id: c
    agent: a
    goal: g
    expect_tools: ['']
""",
            "含有空值",
        ),
    ],
)
def test_load_dataset_bad_data_chinese_error(tmp_path, text, fragment):
    """坏数据一律中文 BusinessError 且带定位片段（绝不静默跳过）。"""
    with pytest.raises(BusinessError) as exc_info:
        load_dataset(_write_dataset(tmp_path, text))
    assert fragment in exc_info.value.msg


def test_load_dataset_missing_file(tmp_path):
    """数据集不存在：1001 中文报错（404 语义）。"""
    with pytest.raises(BusinessError) as exc_info:
        load_dataset(tmp_path / "not-exist.yaml")
    assert "不存在" in exc_info.value.msg


# ---- ② scorer 纯函数路径 ----


def _trajectory(**kwargs) -> Trajectory:
    """评测轨迹构造（默认空答 + 空工具序列，按需覆写）。"""
    base = {"agent": "a", "goal": "g", "status": "DONE", "tools": ()}
    base.update(kwargs)
    return Trajectory(**base)  # type: ignore[arg-type]


def test_tool_selection_exact_and_subsequence():
    """工具选择：exact 序列一致才过；subsequence 按序包含即过。"""
    case = EvalCase(id="c", agent="a", goal="g", expect_tools=("demo.echo", "demo.shout"))
    trajectory = _trajectory(tools=("demo.echo", "demo.shout"))
    assert ToolSelectionScorer().score(case, trajectory)["passed"] is True

    trajectory = _trajectory(tools=("demo.shout", "demo.echo"))
    result = ToolSelectionScorer().score(case, trajectory)
    assert result["passed"] is False
    assert "期望（完全一致）" in result["detail"]

    sub_case = EvalCase(
        id="c", agent="a", goal="g", expect_tools=("demo.echo",), tool_match="subsequence"
    )
    # 期望单步按序包含：实际序列里出现在哪都算（subsequence 不要求相邻）
    trajectory = _trajectory(tools=("demo.shout", "demo.echo"))
    assert ToolSelectionScorer().score(sub_case, trajectory)["passed"] is True
    # 两步期望被实际顺序违反 → 不过
    order_case = EvalCase(
        id="c",
        agent="a",
        goal="g",
        expect_tools=("demo.echo", "demo.shout"),
        tool_match="subsequence",
    )
    trajectory = _trajectory(tools=("demo.shout", "demo.echo"))
    assert ToolSelectionScorer().score(order_case, trajectory)["passed"] is False


def test_tool_selection_skipped_without_expect():
    """未声明 expect_tools → skipped（passed=None），与未过严格区分。"""
    case = EvalCase(id="c", agent="a", goal="g")
    result = ToolSelectionScorer().score(case, _trajectory())
    assert result["passed"] is None
    assert result["score"] == 0.0


def test_number_grounding_reuses_validation():
    """数值溯源：复用 validation 口径；期望数字缺失/失信分别计入 detail。"""
    case = EvalCase(id="c", agent="a", goal="g", expect_numbers=("7",))
    trajectory = _trajectory(
        tools=("demo.shout",),
        results={0: {"shout": "HELLO", "length": 5}},
        answer="长度是 5，不是 7",
    )
    result = NumberGroundingScorer().score(case, trajectory)
    assert result["passed"] is False
    assert "7" in result["detail"]  # 期望数字缺失被点名

    ok_case = EvalCase(id="c", agent="a", goal="g", expect_numbers=("5",))
    trajectory = _trajectory(tools=("demo.shout",), results={0: {"length": 5}}, answer="长度是 5")
    assert NumberGroundingScorer().score(ok_case, trajectory)["passed"] is True


def test_number_grounding_skipped_without_answer():
    """规则快路径无 LLM 终答 → skipped（数值溯源不适用，不误判不过）。"""
    case = EvalCase(id="c", agent="a", goal="g", expect_numbers=("5",))
    result = NumberGroundingScorer().score(case, _trajectory())
    assert result["passed"] is None


def test_answer_keyword_ratio_and_judge_placeholder():
    """关键词按命中比例给分；judge 占位恒 skipped。"""
    case = EvalCase(id="c", agent="a", goal="g", answer_contains=("ok", "missing"))
    result = AnswerKeywordScorer().score(case, _trajectory(answer="this is ok"))
    assert result["score"] == 0.5
    assert result["passed"] is False
    assert "missing" in result["detail"]

    judge = AnswerJudgeScorer().score(case, _trajectory(answer="this is ok"))
    assert judge["passed"] is None
    assert "预留位" in judge["detail"]


# ---- ③ harness 端到端（自备 plugins + conftest 演示工具，零外部依赖） ----


async def test_run_eval_end_to_end(tmp_path):
    """端到端：命中规则的用例 DONE 且工具选择判过；报告形状齐备。"""
    plugins = _write_plugins(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        """
cases:
  - id: echo-ok
    agent: eval-demo
    goal: 来个回声演示
    expect_tools: [demo.echo]
""",
    )
    report = await run_eval(dataset, plugins_dir=plugins, db_path=tmp_path / "eval.db")
    assert report.total == 1
    assert report.passed == 1
    assert report.pass_rate() == 1.0
    case = report.cases[0]
    assert case.status == "DONE"
    assert case.passed is True
    tool_score = next(item for item in case.scores if item["scorer"] == "tool_selection")
    assert tool_score["passed"] is True
    payload = render_json(report)
    assert "echo-ok" in payload and "tool_selection" in payload
    assert "通过率：100.0%" in render_markdown(report)


async def test_run_eval_unmatched_goal_fails_case(tmp_path):
    """目标未命中规则：单条用例未过（状态 FAILED + 错误进报告），不中断整批。"""
    plugins = _write_plugins(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        """
cases:
  - id: miss
    agent: eval-demo
    goal: 帮我预订去月球的机票
    expect_tools: []
""",
    )
    report = await run_eval(dataset, plugins_dir=plugins, db_path=tmp_path / "eval.db")
    assert report.total == 1
    assert report.passed == 0
    case = report.cases[0]
    assert case.status == "FAILED"
    assert case.passed is False
    assert "未命中" in case.error


async def test_run_eval_agent_not_found_is_case_error(tmp_path):
    """智能体不存在：单条用例 error 进报告（中文报错），不抛断整批。"""
    plugins = _write_plugins(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        """
cases:
  - id: ghost
    agent: not-exist-agent
    goal: 随便
""",
    )
    report = await run_eval(dataset, plugins_dir=plugins, db_path=tmp_path / "eval.db")
    assert report.total == 1
    assert report.cases[0].status == "error"
    assert "智能体不存在" in report.cases[0].error
    assert report.cases[0].passed is False


async def test_run_eval_empty_dataset(tmp_path):
    """空数据集：total=0、pass_rate=0.0（CLI 据此返 1，不除零）。"""
    dataset = _write_dataset(tmp_path, "cases: []")
    report = await run_eval(dataset, plugins_dir=_write_plugins(tmp_path))
    assert report.total == 0
    assert report.pass_rate() == 0.0
