"""可插拔 scorer 链（trajectory → ScoreResult）

链路：harness 把每条用例的 Trajectory 依次交给 scorer 链 → 每个 scorer 独立
      产出 {scorer, score, passed, detail} → 汇总为 CaseReport。

口径：
- 三个确定性 scorer 离线可跑（不依赖 LLM）：tool_selection / number_grounding /
  answer_keyword——这也是首个 PR 只做确定性 scorer 的原因：回归信号必须可复现；
- number_grounding 直接复用 validation.validate_answer_numbers（同一套数字词元化
  口径，不另写一份比对逻辑）；
- answer_judge 是 LLM judge 的预留位：未实现 / 调用失败 → passed=None 的
  skipped 结果（项目「降级绝不 500」哲学），绝不拖垮整批评测；
- passed=None 表示 skipped（不适用 / 未配置），与 passed=False（评了，没过）严格区分。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypedDict

from office_agent_runtime.eval.dataset import EvalCase
from office_agent_runtime.validation import validate_answer_numbers

if TYPE_CHECKING:  # 循环 Import 规避：Trajectory 只在注解里出现（PEP 563 延迟求值）
    from office_agent_runtime.eval.harness import Trajectory


class ScoreResult(TypedDict):
    """单个 scorer 对单条用例的评分结果（score∈[0,1]；passed=None 表示 skipped）。"""

    scorer: str
    score: float
    passed: bool | None
    detail: str


class Scorer(Protocol):
    """scorer 窄接口（对齐 vector_store.VectorStore 的 Protocol 惯例）。"""

    name: str

    def score(self, case: EvalCase, trajectory: Trajectory) -> ScoreResult:
        """评一条用例；返回 ScoreResult（skipped 时 score=0.0、passed=None）。"""
        ...


def _skipped(scorer: str, reason: str) -> ScoreResult:
    """skipped 结果统一构造（passed=None 与评鉴未过严格区分）。"""
    return {"scorer": scorer, "score": 0.0, "passed": None, "detail": reason}


def _join(tools: list[str] | tuple[str, ...]) -> str:
    """工具序列的人读呈现（空序列给「（空）」，不显示空箭头）。"""
    return " → ".join(tools) if tools else "（空）"


def _is_subsequence(expected: tuple[str, ...], actual: tuple[str, ...]) -> bool:
    """expected 是否按序包含于 actual（中间可穿插其他工具）。"""
    remaining = iter(actual)
    return all(item in remaining for item in expected)


class ToolSelectionScorer:
    """工具选择正确率：实际成功步骤工具序列 vs 期望序列（exact / subsequence）。"""

    name: str = "tool_selection"

    def score(self, case: EvalCase, trajectory: Trajectory) -> ScoreResult:
        if not case.expect_tools:
            return _skipped(self.name, "用例未声明 expect_tools，工具选择不适用")
        actual = list(trajectory.tools)
        expected = list(case.expect_tools)
        if case.tool_match == "subsequence":
            passed = _is_subsequence(case.expect_tools, trajectory.tools)
            detail = f"期望（按序包含）：{_join(expected)}；实际：{_join(actual)}"
        else:
            passed = actual == expected
            detail = f"期望（完全一致）：{_join(expected)}；实际：{_join(actual)}"
        return {
            "scorer": self.name,
            "score": 1.0 if passed else 0.0,
            "passed": passed,
            "detail": detail,
        }


class NumberGroundingScorer:
    """数值溯源：终答数字能否在工具返回中找到出处（复用 validation 同款口径）。"""

    name: str = "number_grounding"

    def score(self, case: EvalCase, trajectory: Trajectory) -> ScoreResult:
        if not case.expect_numbers:
            return _skipped(self.name, "用例未声明 expect_numbers，数值溯源不适用")
        if not trajectory.answer:
            return _skipped(
                self.name,
                "本次运行无 LLM 终答（规则快路径或未配置 LLM，工具结果即答案），数值溯源不适用",
            )
        # observations 与 trajectory.tools 按位对应（harness 侧同一 ok 条目序构建；
        # strict=False：长度恒等由该不变量保证，评分工件绝不因偶发错位抛错中断整批）
        observations = [
            {"index": position, "tool": tool, "result": trajectory.results.get(key)}
            for position, (key, tool) in enumerate(
                zip(sorted(trajectory.results), trajectory.tools, strict=False)
            )
        ]
        report = validate_answer_numbers(trajectory.answer, observations)
        missing = [number for number in case.expect_numbers if number not in report["checked"]]
        passed = bool(report["passed"]) and not missing
        detail = (
            f"数值校验：{report['label']}；"
            f"期望数字缺失：{'、'.join(missing) if missing else '无'}；"
            f"失信数字：{[item['number'] for item in report['offenders']] or '无'}"
        )
        return {
            "scorer": self.name,
            "score": 1.0 if passed else 0.0,
            "passed": passed,
            "detail": detail,
        }


class AnswerKeywordScorer:
    """终答关键词包含率：answer_contains 命中比例（防「跑通了但答非所问」）。"""

    name: str = "answer_keyword"

    def score(self, case: EvalCase, trajectory: Trajectory) -> ScoreResult:
        if not case.answer_contains:
            return _skipped(self.name, "用例未声明 answer_contains，关键词包含不适用")
        if not trajectory.answer:
            return _skipped(
                self.name,
                "本次运行无 LLM 终答（规则快路径或未配置 LLM），关键词包含不适用",
            )
        hits = [keyword for keyword in case.answer_contains if keyword in trajectory.answer]
        ratio = len(hits) / len(case.answer_contains)
        missing = [keyword for keyword in case.answer_contains if keyword not in hits]
        return {
            "scorer": self.name,
            "score": round(ratio, 4),
            "passed": not missing,
            "detail": (
                f"命中 {len(hits)}/{len(case.answer_contains)}；"
                f"未命中：{'、'.join(missing) if missing else '无'}"
            ),
        }


class AnswerJudgeScorer:
    """LLM judge 预留位：未实现 / 调用失败 → skipped（passed=None，不拖垮整批）。"""

    name: str = "answer_judge"

    def score(self, case: EvalCase, trajectory: Trajectory) -> ScoreResult:
        return _skipped(
            self.name,
            "LLM judge 为预留位尚未实现（本版只提供确定性 scorer，回归信号可复现）",
        )


def default_scorers() -> list[Scorer]:
    """默认 scorer 链（确定性三件 + judge 占位；judge 自动 skip 不影响结果）。"""
    return [
        ToolSelectionScorer(),
        NumberGroundingScorer(),
        AnswerKeywordScorer(),
        AnswerJudgeScorer(),
    ]
