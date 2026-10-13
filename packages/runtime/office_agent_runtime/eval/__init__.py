"""Agent 评测子包（trajectory 评估 harness：数据集 → 执行 → 打分 → 报告）

链路：CLI（``python -m office_agent_runtime.eval <dataset.yaml>``）→ dataset.load_dataset
      读评测用例 → harness.run_eval 逐条 in-process 复用 runner.execute_run 采集
      trajectory → scorers 链打分 → report 渲染 JSON/Markdown。

定位（为什么需要它）：项目已有冒烟测试（功能验证）与数值校验（单次运行内数字溯源），
      但没有任何机制量化「工具选对了吗 / 改 prompt 后行为退化了吗」——本子包补上
      评估环：确定性 scorer 离线可跑，LLM judge 留作扩展位（未配置 LLM 如实 skip）。

红线：
- 纯 additive：不修改 runner / graph / planner 任何现有代码，execute_run 签名
  （approvals 注入契约）冻结不动；
- 降级哲学同项目口径：LLM 未配置 → judge scorer 如实 skipped，绝不报错；
- 报告只陈述事实：不预筛数据、不静默跳过失败用例。
对齐：AGENTS.md §5（验证命令登记）；docs/data-assets/测试与评测类.md（评测集占位的落地）。
"""

from __future__ import annotations

from office_agent_runtime.eval.dataset import EvalCase, load_dataset
from office_agent_runtime.eval.harness import (
    CaseReport,
    EvalReport,
    Trajectory,
    run_eval,
)
from office_agent_runtime.eval.report import render_json, render_markdown
from office_agent_runtime.eval.scorers import (
    AnswerJudgeScorer,
    AnswerKeywordScorer,
    NumberGroundingScorer,
    Scorer,
    ScoreResult,
    ToolSelectionScorer,
    default_scorers,
)

__all__ = [
    "AnswerJudgeScorer",
    "AnswerKeywordScorer",
    "CaseReport",
    "EvalCase",
    "EvalReport",
    "NumberGroundingScorer",
    "ScoreResult",
    "Scorer",
    "ToolSelectionScorer",
    "Trajectory",
    "default_scorers",
    "load_dataset",
    "render_json",
    "render_markdown",
    "run_eval",
]
