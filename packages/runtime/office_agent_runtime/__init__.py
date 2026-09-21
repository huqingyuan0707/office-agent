"""office-agent 智能体运行时公开 API

编排层定位：用户给一句自然语言目标，planner 规划成一串工具调用，逐步执行。
纪律只有一条——planner 只能「提议」，每一步都走内核 executor.call，
Scope 硬拦 / Schema 校验 / 熔断 / 审计 / 溯源自动生效，编排层绕不过治理口径。

典型用法（宿主可选装配，见 ext.mount）::

    from office_agent_runtime.ext import mount
    mount(app)  # 挂 /api/v1/agents 与 /api/v1/runs

各子模块按需导入；此处只做便捷重导出（F401 已在 ruff per-file-ignores 放行）。
"""

from office_agent_runtime.ext import mount
from office_agent_runtime.loader import (
    agent_config_paths,
    agents_dir,
    find_agent_spec,
    load_agent_specs,
)
from office_agent_runtime.models import RunStep
from office_agent_runtime.planner import RulePlanner
from office_agent_runtime.runner import RUN_TASK_TYPE, execute_run, start_run
from office_agent_runtime.spec import AgentSpec, PlannerRule, PlannerStep, parse_agent_spec

__all__ = [
    "RUN_TASK_TYPE",
    "AgentSpec",
    "PlannerRule",
    "PlannerStep",
    "RulePlanner",
    "RunStep",
    "agent_config_paths",
    "agents_dir",
    "execute_run",
    "find_agent_spec",
    "load_agent_specs",
    "mount",
    "parse_agent_spec",
    "start_run",
]
