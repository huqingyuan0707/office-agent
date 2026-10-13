"""评测数据集加载与校验（YAML → EvalCase）

链路：数据集 YAML（cases 列表）→ load_dataset 逐条校验 → harness 消费。

口径（对齐 spec.py 的 parse_agent_spec 纪律）：
- 坏数据一律中文 BusinessError（1001），带条目序号，绝不静默跳过——
  评测集本身写错必须让使用者立刻知道，而不是悄悄少跑几条；
- expect 全部可选：只写工具的用例评工具选择，只写数字的评数值溯源；
- tool_match 只认 exact（序列完全一致）/ subsequence（期望序列按序包含）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from office_agent_core.errors import BusinessError, ErrorCode

#: tool_match 合法值（exact = 完全一致；subsequence = 期望工具按序包含于实际序列）
_TOOL_MATCH_MODES = ("exact", "subsequence")


@dataclass(frozen=True)
class EvalCase:
    """一条评测用例（数据集的 YAML 条目内存形态）。"""

    id: str
    agent: str
    goal: str
    expect_tools: tuple[str, ...] = ()
    tool_match: str = "exact"
    expect_numbers: tuple[str, ...] = ()
    answer_contains: tuple[str, ...] = ()


def load_dataset(path: Path) -> list[EvalCase]:
    """加载评测数据集（YAML 顶层 ``cases`` 列表）；坏数据中文报错，空集返回空列表。"""
    if not path.is_file():
        raise BusinessError(ErrorCode.PARAM_INVALID, f"评测数据集不存在：{path}", 404)
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"评测数据集 YAML 解析失败：{path}：{str(exc)[:200]}",
        ) from None
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, "评测数据集顶层必须是键值对（含 cases 列表）")
    cases_raw = raw.get("cases")
    if cases_raw is None:
        return []
    if not isinstance(cases_raw, list):
        raise BusinessError(ErrorCode.PARAM_INVALID, "评测数据集的 cases 必须是列表")
    return [_parse_case(item, position) for position, item in enumerate(cases_raw, start=1)]


def _parse_case(raw: Any, position: int) -> EvalCase:
    """解析并校验单条用例（position 从 1 起，用于错误定位）。"""
    if not isinstance(raw, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, f"第 {position} 条评测用例必须是键值对")
    case_id = str(raw.get("id") or "").strip()
    if not case_id:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"第 {position} 条评测用例缺少 id")
    agent = str(raw.get("agent") or "").strip()
    if not agent:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"第 {position} 条评测用例（{case_id}）缺少 agent"
        )
    goal = str(raw.get("goal") or "").strip()
    if not goal:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"第 {position} 条评测用例（{case_id}）缺少 goal"
        )
    tool_match = str(raw.get("tool_match") or "exact").strip() or "exact"
    if tool_match not in _TOOL_MATCH_MODES:
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"第 {position} 条评测用例（{case_id}）的 tool_match 只认 "
            f"{' / '.join(_TOOL_MATCH_MODES)}，收到：{tool_match}",
        )
    return EvalCase(
        id=case_id,
        agent=agent,
        goal=goal,
        expect_tools=_str_list(raw.get("expect_tools"), "expect_tools", case_id),
        tool_match=tool_match,
        expect_numbers=_str_list(raw.get("expect_numbers"), "expect_numbers", case_id),
        answer_contains=_str_list(raw.get("answer_contains"), "answer_contains", case_id),
    )


def _str_list(raw: Any, field_name: str, case_id: str) -> tuple[str, ...]:
    """期望列表字段解析（去重保序；None 给空元组；空值/非列表中文报错）。"""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"评测用例 {case_id} 的 {field_name} 必须是列表"
        )
    items: list[str] = []
    for item in raw:
        text = str(item if item is not None else "").strip()
        if not text:
            raise BusinessError(
                ErrorCode.PARAM_INVALID, f"评测用例 {case_id} 的 {field_name} 含有空值"
            )
        items.append(text)
    return tuple(dict.fromkeys(items))  # 去重保序（重复期望没有语义，疑似手误）
