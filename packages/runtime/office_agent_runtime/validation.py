"""回答数值校验（R2：数值不可编造的基础版）+ LLM 终答合成钩子

链路：run 收敛 DONE 后 → finalize_answer() 用 spec.llm 做一次终答合成（把本次 run
      全部工具返回 JSON 交给 LLM 生成中文回答）→ validate_answer_numbers() 抽取回答中
      的数字，逐一在工具返回 JSON 里找出处 → 校验标记随 checkpoint / run 概要透出。

红线：
- 不删除回答：未通过校验的回答原样保留并标「未通过数值校验」（标注失信，不静默改写）；
- 降级不 500：LLM 未配置 / 调用失败只置 skipped 标记，绝不阻断已成功的工具步骤；
- 校验是纯函数：数字两边用同一套词元化（\d+(?:\.\d+)?，忽略正负号），字符串里的
  数字（日期、时间、百分比）与数值叶子统一比对，可单测、可回放。
对齐：.trae/documents/智能体编排层实现方案.md §R2（回答校验）。
"""

from __future__ import annotations

import re
from typing import Any

from office_agent_runtime.planner.llm import LlmFunctionCallPlanner, LlmPlanError
from office_agent_runtime.spec import AgentSpec

#: 数字词元（不含正负号：日期「2026-09-22」拆成 2026/09/22，与工具串内词元同构可比）
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

#: 校验报告里附带的原始数据（evidence）条数上限，防超长工具返回把 checkpoint 撑爆
_EVIDENCE_LIMIT = 50


def validate_answer_numbers(answer: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    """校验回答中的每个数字都能在本次 run 工具返回 JSON 里找到出处。

    observations 形如 [{"index": 0, "tool": "...", "result": <工具返回 JSON>}]。
    返回校验报告：passed / label / checked / offenders / evidence（原始数据数值面）。
    """
    evidence: list[dict[str, Any]] = []
    values: list[float] = []
    for obs in observations:
        try:
            index = int(obs.get("index"))
        except (TypeError, ValueError):
            continue
        _collect_numbers(obs.get("result"), f"steps[{index}].result", evidence, values)

    checked: list[str] = []
    offenders: list[dict[str, Any]] = []
    for raw in _NUMBER_RE.findall(answer or ""):
        number = float(raw)
        checked.append(raw)
        if not any(abs(number - value) < 1e-9 for value in values):
            offenders.append({"number": raw, "note": "未能在本次运行的工具返回数据中找到该数字"})
    passed = not offenders
    return {
        "applicable": True,
        "passed": passed,
        "label": "已通过数值校验" if passed else "未通过数值校验",
        "checked": checked,
        "offenders": offenders,
        "evidence": evidence[:_EVIDENCE_LIMIT],
    }


def _collect_numbers(
    node: Any, path: str, evidence: list[dict[str, Any]], values: list[float]
) -> None:
    """递归收集工具返回 JSON 里的全部数字词元（数值叶子 + 字符串内嵌数字）。"""
    if isinstance(node, bool) or node is None:
        return
    if isinstance(node, (int, float)):
        for raw in _NUMBER_RE.findall(str(node)):
            _append_evidence(path, raw, evidence, values)
        return
    if isinstance(node, str):
        for raw in _NUMBER_RE.findall(node):
            _append_evidence(path, raw, evidence, values)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            _collect_numbers(value, f"{path}.{key}", evidence, values)
        return
    if isinstance(node, list):
        for position, value in enumerate(node):
            _collect_numbers(value, f"{path}[{position}]", evidence, values)


def _append_evidence(
    path: str, raw: str, evidence: list[dict[str, Any]], values: list[float]
) -> None:
    """登记一个数字出处（同路径同值去重，防重复条目刷屏）。"""
    number = float(raw)
    for item in evidence:
        if item["path"] == path and abs(item["value"] - number) < 1e-9:
            return
    evidence.append({"path": path, "value": number})
    values.append(number)


async def finalize_answer(
    *, spec: AgentSpec, goal: str, results: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    """run 收敛 DONE 后的终答合成 + 数值校验标注（降级绝不阻断）。

    - 未配置 LLM → 校验不适用标记（规则规划没有 LLM 回答，数字本就来自工具模板）；
    - LLM 失败 → skipped 标记，回答留空，已完成的步骤不受影响；
    - 成功 → answer 原文 + validate_answer_numbers 报告（未通过也原样保留回答）。
    """
    if not spec.llm:
        return {
            "answer": "",
            "validation": {
                "applicable": False,
                "passed": None,
                "label": "数值校验不适用",
                "reason": "该智能体未配置 LLM，无 LLM 生成回答（数字均来自工具模板直出）",
            },
        }
    observations = [
        {"index": index, "tool": str(outcome.get("tool") or ""), "result": outcome.get("result")}
        for index, outcome in sorted(results.items())
    ]
    planner = LlmFunctionCallPlanner.from_spec(spec)
    try:
        answer = await planner.answer(goal, observations)
    except LlmPlanError as exc:
        return {
            "answer": "",
            "validation": {
                "applicable": True,
                "skipped": True,
                "passed": None,
                "label": "未校验（LLM 生成回答失败，已降级不阻断）",
                "reason": exc.msg,
            },
        }
    finally:
        await planner.aclose()
    report = validate_answer_numbers(answer, observations)
    return {"answer": answer, "validation": report}
