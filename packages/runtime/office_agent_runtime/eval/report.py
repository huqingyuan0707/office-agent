"""评测报告渲染（EvalReport → JSON / Markdown）

链路：harness.run_eval 产出 EvalReport → render_json（机器可读，供 CI 存档与
      跨版本对比）/ render_markdown（人读，贴 PR 描述或 Issue）。

口径：报告只陈述事实——skipped 的 scorer 如实标注（passed=None），失败用例的
      明细（期望 vs 实际工具序列、失信数字）必须可见，不做任何粉饰。
"""

from __future__ import annotations

import json

from office_agent_runtime.eval.harness import CaseReport, EvalReport


def render_json(report: EvalReport) -> str:
    """机器可读报告（ensure_ascii=False，中文原样；缩进 2，便于 diff）。"""
    payload = {
        "total": report.total,
        "passed": report.passed,
        "pass_rate": round(report.pass_rate(), 4),
        "cases": [_case_payload(case) for case in report.cases],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_markdown(report: EvalReport) -> str:
    """人读报告（汇总行 + 用例表格 + 失败明细；skipped 与未过都如实呈现）。"""
    lines = [
        "# Agent 评测报告",
        "",
        f"- 用例总数：{report.total}；通过：{report.passed}；"
        f"通过率：{report.pass_rate() * 100:.1f}%",
        "",
        "| 用例 | 智能体 | 状态 | 结果 | 未过 scorer |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case in report.cases:
        failed = [item["scorer"] for item in case.scores if item["passed"] is False]
        lines.append(
            f"| {case.case_id} | {case.agent} | {case.status} | "
            f"{'通过' if case.passed else '未通过'} | {'、'.join(failed) or '—'} |"
        )
    failures = [case for case in report.cases if not case.passed]
    if failures:
        lines.extend(["", "## 失败明细", ""])
        for case in failures:
            lines.append(f"### {case.case_id}（{case.agent}）")
            lines.append(f"- 目标：{case.goal}")
            if case.error:
                lines.append(f"- 错误：{case.error}")
            elif not any(item["passed"] is False for item in case.scores):
                lines.append(f"- 状态：{case.status}（运行未成功收敛为 DONE，无 scorer 判过）")
            for item in case.scores:
                if item["passed"] is False:
                    lines.append(f"- {item['scorer']}：{item['detail']}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _case_payload(case: CaseReport) -> dict:
    """单条用例的 JSON 面（scores 原样透出，skipped 不粉饰）。"""
    return {
        "id": case.case_id,
        "agent": case.agent,
        "goal": case.goal,
        "status": case.status,
        "passed": case.passed,
        "error": case.error,
        "scores": [dict(item) for item in case.scores],
    }
