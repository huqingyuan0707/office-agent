"""跨智能体路由（对话主入口：员工一句话 → 自动挑智能体）

链路：POST /runs（agent 省略）→ route_agent_spec(goal) → 命中 AgentSpec → start_run 照常受理。
两轮扫描，规则优先：
  1) 规则智能体（有 rules）：goal 命中任一规则关键词即选中（RulePlanner.plan 非空，
     与执行期规划同一匹配口径，路由选中的智能体执行期规划必然成功，不出现「路由说行、执行说不行」）；
  2) LLM 智能体（llm profile 非空）：规则全不中时兜底——由 LLM 决定工具调用，
     LLM 不可用时执行期降级链自行收敛（规划失败在时间线里如实呈现，路由不预判）。
全不中：1001 中文可操作报错，列出已装载智能体及其能力描述，绝不瞎猜一个。

红线：路由只「挑智能体」不执行任何工具；纯函数零 IO（specs 由调用方所在模块注入），
     不触及 ORM / FastAPI；与内核「planner 只提议不执行」同纪律。
对齐：AGENTS.md §3（分层红线）；.trae/documents/智能体编排层实现方案.md §4
     （POST /runs 契约：agent 可选，省略即自动路由）；PRD §4.3（纯自然语言交互零门槛）。
"""

from __future__ import annotations

from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_runtime.loader import load_agent_specs
from office_agent_runtime.planner.llm import profile_configured
from office_agent_runtime.planner.rule import RulePlanner
from office_agent_runtime.spec import AgentSpec


def route_agent_spec(goal: str) -> AgentSpec:
    """按目标为一句话挑智能体：规则命中优先，LLM 智能体兜底，全不中 1001。

    specs 每次实扫 plugins/*/agent.yaml（与清单/受理同口径，配置热更新零重启）；
    扫描顺序稳定（loader 按目录名排序），命中多个规则时取声明序第一个，可预期。
    LLM 兜底只挑 profile 确已配置的智能体（零网络查 LLM_PROVIDERS）——没配就不接，
    让用户立刻看到「没人能办」而不是等一轮执行期失败。
    """
    specs = load_agent_specs()
    for spec in specs:
        if spec.rules and RulePlanner(spec).plan(goal):
            return spec
    for spec in specs:
        if spec.llm and profile_configured(spec.llm):
            return spec
    known = "；".join(f"{spec.name}（{spec.description}）" for spec in specs) or "暂无已装载智能体"
    raise BusinessError(
        ErrorCode.PARAM_INVALID,
        f"没有智能体能处理这件事：「{goal[:60]}」。已装载智能体：{known}，"
        "请换个说法或联系管理员补充智能体规则",
    )
