"""AgentSpec（定制化智能体的全部声明）与解析校验

链路：loader 读 plugins/*/agent.yaml → parse_agent_spec 校验 → runner / planner 消费。

口径：定制化 = 声明式配置而非代码——system prompt、工具白名单、步数上限、规划规则
     全部在这里声明，主包零 prompt、零业务词。校验失败抛中文 BusinessError（1001），
     绝不静默降级；rules 里的步骤工具必须是白名单内的名字，白名单之外直接拒。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from office_agent_core.errors import BusinessError, ErrorCode


@dataclass(frozen=True)
class PlannerStep:
    """一步「提议」（planner 只提议不执行；真正执行在 runner 交内核 executor.call）。"""

    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class PlannerRule:
    """一条规划规则：goal 命中 match 任一关键词即按 steps 模板逐步提议。"""

    match: tuple[str, ...]
    steps: tuple[PlannerStep, ...]


@dataclass(frozen=True)
class AgentSpec:
    """智能体声明（agent.yaml 的内存形态）。

    llm 为空串 = 未配置 LLM，走规则规划（R1 接入 LlmFunctionCallPlanner 后
    该字段作为 LLM profile 名，未配置且无 rules 时创建 run 明确中文报错）。

    llm_mode 声明 LLM 规划的工作方式：plan（默认）= 单轮一次性出全计划（零回归）；
    react = ReAct 逐步再规划——每步执行完拿真实出参再提议下一步（解决单轮规划
    拿不到上一步结果、参数只能编造的边界，见 AGENTS §6）。字段与 LLM 是否配置
    正交：llm 为空时 react 无从生效，由 planner 构造侧按降级链处理。
    """

    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...]
    max_steps: int
    llm: str
    rules: tuple[PlannerRule, ...]
    llm_mode: str = "plan"

    def to_dict(self) -> dict[str, Any]:
        """清单出参（不含 system_prompt：清单展示不需要，prompt 留给执行链路）。"""
        return {
            "name": self.name,
            "description": self.description,
            "tools": list(self.tools),
            "max_steps": self.max_steps,
            "llm": self.llm,
            "llm_mode": self.llm_mode,
            "rule_count": len(self.rules),
        }


def compose_context_goal(context: str, goal: str) -> str:
    """多轮上下文目标拼接（路由与规划**同一口径**，见 router 红线）。

    context 非空时拼接为「前文上下文 + 用户新目标」，使「上面那个/再加一个/改成张三」
    这类指代能同时命中路由规则与规划规则；context 为空时原样返回新目标（首轮行为零变化，
    与 POST /runs 直发完全一致）。四处共用：路由 route_agent_spec / 规划 prepare_plan /
    ReACT plan_next / 会话 API 发消息。
    """
    trimmed = (context or "").strip()
    if not trimmed:
        return goal
    return f"{trimmed}\n\n本次目标：{goal}"


def parse_agent_spec(raw: Any) -> AgentSpec:
    """解析并校验 agent.yaml 文档（非法配置一律中文报错，绝不静默降级）。"""
    if not isinstance(raw, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, "智能体配置必须是键值对（YAML 顶层映射）")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise BusinessError(ErrorCode.PARAM_INVALID, "智能体配置缺少 name，且不能为空白")

    tools_raw = raw.get("tools")
    if not isinstance(tools_raw, list) or not tools_raw:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"智能体 {name} 的 tools 白名单不能为空")
    tools: list[str] = []
    for item in tools_raw:
        tool = str(item or "").strip()
        if not tool:
            raise BusinessError(
                ErrorCode.PARAM_INVALID, f"智能体 {name} 的 tools 白名单含有空工具名"
            )
        if tool not in tools:
            tools.append(tool)

    max_steps = raw.get("max_steps", 1)
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"智能体 {name} 的 max_steps 必须是不小于 1 的整数"
        )

    llm_mode = str(raw.get("llm_mode") or "plan").strip() or "plan"
    if llm_mode not in ("plan", "react"):
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"智能体 {name} 的 llm_mode 只认 plan（单轮规划）或 react（逐步再规划），"
            f"收到：{llm_mode}",
        )

    rules = _parse_rules(name, frozenset(tools), raw.get("rules"))
    return AgentSpec(
        name=name,
        description=str(raw.get("description") or ""),
        system_prompt=str(raw.get("system_prompt") or ""),
        tools=tuple(tools),
        max_steps=max_steps,
        llm=str(raw.get("llm") or "").strip(),
        rules=rules,
        llm_mode=llm_mode,
    )


def _parse_rules(name: str, whitelist: frozenset[str], raw: Any) -> tuple[PlannerRule, ...]:
    """解析 rules（可选）：match 非空、steps 非空、步骤工具必须在白名单内。"""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise BusinessError(ErrorCode.PARAM_INVALID, f"智能体 {name} 的 rules 必须是列表")
    rules: list[PlannerRule] = []
    for rule_no, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise BusinessError(
                ErrorCode.PARAM_INVALID, f"智能体 {name} 的第 {rule_no} 条规则必须是键值对"
            )
        match_raw = item.get("match")
        if not isinstance(match_raw, list) or not match_raw:
            raise BusinessError(
                ErrorCode.PARAM_INVALID, f"智能体 {name} 的第 {rule_no} 条规则 match 不能为空"
            )
        keywords = tuple(str(keyword or "").strip() for keyword in match_raw)
        if any(not keyword for keyword in keywords):
            raise BusinessError(
                ErrorCode.PARAM_INVALID, f"智能体 {name} 的第 {rule_no} 条规则 match 含空关键词"
            )
        steps_raw = item.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise BusinessError(
                ErrorCode.PARAM_INVALID, f"智能体 {name} 的第 {rule_no} 条规则 steps 不能为空"
            )
        steps: list[PlannerStep] = []
        for step_no, step in enumerate(steps_raw, start=1):
            if not isinstance(step, dict):
                raise BusinessError(
                    ErrorCode.PARAM_INVALID,
                    f"智能体 {name} 的第 {rule_no} 条规则第 {step_no} 步必须是键值对",
                )
            tool = str(step.get("tool") or "").strip()
            args = step.get("args") or {}
            if not tool:
                raise BusinessError(
                    ErrorCode.PARAM_INVALID,
                    f"智能体 {name} 的第 {rule_no} 条规则第 {step_no} 步缺少 tool",
                )
            if tool not in whitelist:
                raise BusinessError(
                    ErrorCode.PARAM_INVALID,
                    f"智能体 {name} 的第 {rule_no} 条规则第 {step_no} 步引用了白名单外的工具"
                    f" {tool}（白名单：{'、'.join(sorted(whitelist))}）",
                )
            if not isinstance(args, dict):
                raise BusinessError(
                    ErrorCode.PARAM_INVALID,
                    f"智能体 {name} 的第 {rule_no} 条规则第 {step_no} 步的 args 必须是键值对",
                )
            steps.append(PlannerStep(tool=tool, args=args))
        rules.append(PlannerRule(match=keywords, steps=tuple(steps)))
    return tuple(rules)
