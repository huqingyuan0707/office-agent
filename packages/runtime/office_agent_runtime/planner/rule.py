"""规则规划器（零 LLM 可演示、可单测；LLM 规划器属 R1）

链路：runner 用 AgentSpec 构造本规划器 → plan(goal) 关键词命中返回步骤模板
      → resolve_args 把参数模板里的 ``{steps[N].result.路径}`` 替换为历史步骤结果。

红线：planner 只「提议」（tool + args 模板），永远不执行；每一步都由 runner 交内核
      executor.call，白名单越权只会被拒，不存在绕过执行器的路径。
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_runtime.spec import AgentSpec, PlannerStep

#: 取值模板：{steps[N].result.路径}（路径可省略 = 整个 result；支持字典键与数组下标）
_TEMPLATE = re.compile(r"\{steps\[(\d+)\]\.result(?:\.([^{}]+))?\}")


class RulePlanner:
    """规则规划器：goal 命中 rules[].match 任一关键词即按该规则逐步提议。"""

    def __init__(self, spec: AgentSpec) -> None:
        self._spec = spec

    def plan(self, goal: str) -> list[PlannerStep]:
        """返回命中规则的步骤模板（深拷贝副本，调用方改动不污染声明）；未命中返回空列表。"""
        for rule in self._spec.rules:
            if any(keyword and keyword in goal for keyword in rule.match):
                return [
                    PlannerStep(tool=step.tool, args=deepcopy(step.args)) for step in rule.steps
                ]
        return []

    @staticmethod
    def resolve_args(args: dict[str, Any], results: dict[int, dict[str, Any]]) -> dict[str, Any]:
        """填充参数模板：results 是「步号 → executor.call 完整出参」的历史。

        取不到值（步号尚未产生结果 / 结果里没有该字段）抛中文 BusinessError，
        绝不静默用空值顶替——编造参数是编排层红线。
        """
        return {key: _resolve(value, results) for key, value in args.items()}


def _resolve(node: Any, results: dict[int, dict[str, Any]]) -> Any:
    """递归解析：字符串走模板替换，字典/列表逐元素下钻，标量原样返回。"""
    if isinstance(node, str):
        return _resolve_text(node, results)
    if isinstance(node, dict):
        return {key: _resolve(value, results) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve(value, results) for value in node]
    return node


def _resolve_text(text: str, results: dict[int, dict[str, Any]]) -> Any:
    """单字符串解析：整串即单个模板 → 原类型透传；否则按文本替换（非字符串转 JSON 文本）。"""
    matches = list(_TEMPLATE.finditer(text))
    if not matches:
        return text

    def _value_of(match: re.Match[str]) -> Any:
        index = int(match.group(1))
        outcome = results.get(index)
        if outcome is None:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"步骤 {index} 还没有结果，无法填充参数模板「{match.group(0)}」",
            )
        return _lookup(index, outcome.get("result"), (match.group(2) or "").strip(), match.group(0))

    if len(matches) == 1 and matches[0].span() == (0, len(text)):
        return _value_of(matches[0])
    out = text
    for match in matches:
        value = _value_of(match)
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        out = out.replace(match.group(0), value)
    return out


def _lookup(index: int, result: Any, path: str, template: str) -> Any:
    """按点路径在步骤结果里取值（字典按键、列表按下标），取不到抛中文 BusinessError。"""
    node: Any = result
    for segment in (segment.strip() for segment in path.split(".") if segment.strip()):
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        elif isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
            node = node[int(segment)]
        else:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"步骤 {index} 的结果里找不到字段「{path}」，无法填充 {template}",
            )
    return node
