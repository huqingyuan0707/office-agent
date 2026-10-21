"""规划期决策（计划回放 + 降级链选择），从执行态宿主里拆出（体量红线：loop_state 归零）。

职责：
- plan_from_checkpoint()：检查点里的持久化计划 → (PlannerStep 列表, planner_source)；
- choose_and_plan()：降级链——plan 模式 LLM 优先 → 失败降 Rule → 无规则报错；
  react 模式（混合串联）规则快路径优先，未中且 LLM 已配置则空计划起步由逐步再规划补。

链路：graph.py plan 节点 → LoopState.prepare_plan → 本模块 → planner/* → 回 LoopState 执行。

口径与决策记录见 runner.py 模块 docstring（受理与执行分离 / 降级链 / react 增量计划）；
本模块只做「选计划」，不执行任何工具、不碰数据库。
对齐：AGENTS.md §3（分层红线：业务进纯函数模块）；智能办公Agent 产品需求文档.md §7.2。
"""

from __future__ import annotations

import logging
from typing import Any

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_runtime.planner.llm import (
    LlmFunctionCallPlanner,
    LlmPlanError,
    profile_configured,
)
from office_agent_runtime.planner.rule import RulePlanner
from office_agent_runtime.spec import AgentSpec, PlannerStep

logger = logging.getLogger(__name__)


def plan_from_checkpoint(checkpoint: dict[str, Any]) -> tuple[list[PlannerStep] | None, str]:
    """检查点里的持久化计划 → (PlannerStep 列表, planner_source) 元组。

    返回 (None, "") 表示无持久化计划；损坏的 plan 给 None 让调用方重规划。
    续跑回放「原计划 + 原来源」而非按当前 agent.yaml 重规划：agent.yaml 在
    失败与续跑之间被修改时，重规划的步号与 checkpoint 回放的历史结果会错位；
    工具级白名单仍按当前 spec 在执行前硬拦。
    """
    raw = checkpoint.get("plan")
    src = str(checkpoint.get("planner_source") or "").strip() or "rule"
    if not isinstance(raw, list) or not raw:
        return None, ""
    steps: list[PlannerStep] = []
    for entry in raw:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("tool"), str)
            or not isinstance(entry.get("args"), dict)
        ):
            raise BusinessError(
                ErrorCode.PARAM_INVALID, "检查点中的计划数据损坏，无法续跑；请重新发起运行"
            )
        steps.append(PlannerStep(tool=entry["tool"], args=entry["args"]))
    return steps, src


async def choose_and_plan(spec: AgentSpec, goal: str) -> tuple[list[PlannerStep], str]:
    """降级链：plan 模式 LLM 优先 → 失败降 Rule → 无规则报错（不静默编造）。

    react 模式（混合串联）：规则快路径优先（关键词命中走确定性链，链内文档步
    已切大模型成稿）；未中且 LLM 已配置 → 返回 ([], "react") 空计划起步，步骤由
    LoopState.plan_next 逐步增量补；profile 未配置在受理即报错（受理与执行分离，
    不让用户等一轮执行才看到失败）。
    返回 (planned_steps, planner_source)；source 是 "llm" / "rule" / "react"。
    LLM 出站客户端只在本次规划内使用，finally 里关闭（自建连接不跨调用滞留）。
    """
    if spec.llm_mode == "react":
        if spec.rules:
            steps = RulePlanner(spec).plan(goal)
            if steps:
                return steps, "rule"
        if not spec.llm:
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"智能体 {spec.name} 的 llm_mode=react 但未配置 llm，"
                f"且目标未命中任何规则，无法执行",
            )
        if not profile_configured(spec.llm):
            raise BusinessError(
                ErrorCode.LLM_FAILED,
                f"智能体 {spec.name} 的 llm_mode=react 且目标未命中规则，"
                f"但 LLM profile「{spec.llm}」未在 LLM_PROVIDERS 配置，无法执行",
            )
        return [], "react"
    if spec.llm:
        planner = LlmFunctionCallPlanner.from_spec(spec)
        try:
            steps = await planner.plan(goal)
        except LlmPlanError as exc:
            if not spec.rules:
                raise BusinessError(
                    ErrorCode.LLM_FAILED,
                    f"智能体 {spec.name} 指定了 LLM 规划（profile={spec.llm}）"
                    f"但 LLM 不可用，且未配置 rules 规则规划，无法执行：{exc.msg}",
                ) from None
            logger.warning(
                "LLM planner 规划失败：%s（agent=%s），降级 RulePlanner", exc.msg, spec.name
            )
        else:
            if steps:
                return steps, "llm"
            # LLM 返回空（无 tool_calls）：视为不可用，降级规则规划
            logger.warning("LLM planner 未返回 tool_calls（agent=%s），降级 RulePlanner", spec.name)
        finally:
            await planner.aclose()
    if spec.rules:
        return RulePlanner(spec).plan(goal), "rule"
    raise BusinessError(
        ErrorCode.PARAM_INVALID,
        f"智能体 {spec.name} 未配置 llm 也未配置 rules，无法规划",
    )
