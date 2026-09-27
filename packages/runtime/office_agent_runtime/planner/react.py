"""ReAct 逐步再规划器（llm_mode: react 的规划引擎）

链路：loop_state.plan_next → ReACTPlanner.next_step(goal, results) → 把全部已完成
      步骤的真实出参（截断摘要）交给 LLM → 返回恰好一条 PlannerStep；LLM 不再
      提议工具（纯文本）→ 返回 None（收工，进终答合成）。

与单轮规划（LlmFunctionCallPlanner.plan）的差异：plan 一次性提议全部步骤，拿不到
      上一步真实出参，参数只能靠模型猜（AGENTS §6 登记的边界）；react 每步执行完
      再问一次，args 引用真实数据——「数字不经模型手抄」红线在编排层的落点。

降级口径：出站失败抛 LlmPlanError（与 plan 同型），由 loop_state 决定降级（切规则
      链兜底或收口），本层不吞错。复用方式：继承 LlmFunctionCallPlanner（出站/
      客户端管理/profile 解析零复制，llm.py 已顶 400 行门禁不再加行）。

对齐：.trae/documents/对话办理重构-大模型串联三场景.md §机制件 4。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from office_agent_runtime.planner.llm import (
    LlmFunctionCallPlanner,
    LlmPlanError,
    _business_today,
    _extract_tool_calls,
    _provider_of,
    _tool_schemas,
)
from office_agent_runtime.planner.llm_opts import build_chat_payload
from office_agent_runtime.spec import PlannerStep

logger = logging.getLogger(__name__)

#: 单步结果 JSON 的截断上限（字符）——本地小模型上下文窗口有限（Ollama 8192），
#: 全量出参会顶爆上下文；截断保留可读摘要，编排层不需要 LLM 复述完整原文
_MAX_RESULT_CHARS = 800


class ReACTPlanner(LlmFunctionCallPlanner):
    """ReAct 规划器：逐步看真实出参再提议下一步（plan 的姊妹实现，共用出站链路）。"""

    async def next_step(self, goal: str, results: dict[int, dict[str, Any]]) -> PlannerStep | None:
        """看「步号 → executor 完整出参」的全部历史，提议下一条工具调用。

        - LLM 返回 tool_calls → 取第一条转 PlannerStep（一次只走一步，多条告警截取）；
        - LLM 返回纯文本（不再调工具）→ None（收工信号，loop_state 进终答合成）；
        - 出站/解析失败 → 抛 LlmPlanError，调用方决定降级，绝不静默编一步。
        """
        cfg = _provider_of(self._profile_name)
        tools = _tool_schemas(self._spec)
        if not tools:
            raise LlmPlanError(
                f"智能体 {self._spec.name} 的工具白名单里没有已注册的工具，"
                f"ReAct 规划器无法构造 tools 参数"
            )

        observations = _observations(results)
        messages: list[dict[str, Any]] = []
        if self._spec.system_prompt:
            messages.append({"role": "system", "content": self._spec.system_prompt})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"今天是 {_business_today()}。目标：{goal}\n\n"
                    f"已完成步骤与真实结果（JSON，唯一数据来源）：\n"
                    f"{json.dumps(observations, ensure_ascii=False)}\n\n"
                    "请决定下一步：还需要调用工具，就通过 tool_calls 返回恰好一条调用，"
                    "args 必须引用上述真实结果里的值（数字与字段值禁止编造、禁止推算）；"
                    "目标已可完成，就直接回复文本「完成」，不要再调用工具。"
                ),
            }
        )

        payload = build_chat_payload(cfg.model, messages, tools)
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"

        data = await self._post_chat(cfg, payload, headers)
        step = _first_step(data, allowed_tools=frozenset(self._spec.tools))
        if step is None:
            logger.info("ReAct 规划器判定收工（LLM 未再提议工具），goal=%s", goal[:80])
        return step


def _observations(results: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """历史出参 → 截断摘要（步号升序）：index / tool / status / result(JSON 文本)。

    result 超长截断（防顶爆小模型上下文），带「已截断」标记绝不伪装完整；
    出参缺 result 键按 null 呈现（FAILED 步骤也要让 LLM 看到失败事实）。
    """
    out: list[dict[str, Any]] = []
    for index in sorted(results):
        outcome = results[index]
        result_text = json.dumps(outcome.get("result"), ensure_ascii=False, default=str)
        if len(result_text) > _MAX_RESULT_CHARS:
            result_text = result_text[:_MAX_RESULT_CHARS] + "…（已截断）"
        out.append(
            {
                "index": index,
                "tool": str(outcome.get("tool") or ""),
                "status": str(outcome.get("status") or ""),
                "result": result_text,
            }
        )
    return out


def _first_step(data: dict[str, Any], *, allowed_tools: frozenset[str]) -> PlannerStep | None:
    """从 chat.completions 响应取「恰好一条」提议；纯文本（无 tool_calls）→ None。

    复用 _extract_tool_calls 做解析与白名单校验（不复制 JSON/白名单逻辑）；
    它对「无 tool_calls」抛错——ReAct 里那是收工信号，所以先探测再调用。
    LLM 一次提议多条时只告警并取第一条（ReAct 节奏 = 一步一问，不并走）。
    """
    choices = data.get("choices")
    if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
        raise LlmPlanError("LLM 响应缺少 choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise LlmPlanError("LLM 响应缺少 message")
    tool_calls = message.get("tool_calls")
    if not (isinstance(tool_calls, list) and tool_calls):
        return None
    steps = _extract_tool_calls(data, allowed_tools=allowed_tools)
    if len(steps) > 1:
        logger.warning("ReAct 一次只执行一步，LLM 却提议了 %d 步，取第一条", len(steps))
    return steps[0]
