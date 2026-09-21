"""office-agent-tools-office：内置办公工具包独立 pip 包。

职责：注册三个办公域工具——
① office.report.generate：结构化中文日报（读，不调大模型，每个数字带溯源标注）；
② office.schedule.view：查询日程视图（读，带 source+fetched_at 溯源）；
③ office.todo.create：创建待办（写，scope=office:write + requires_approval=True + 幂等）。

链路：server lifespan → 本包 register_all() → registry 注册 ToolSpec；executor 按 spec 执行。
对齐：AGENTS.md §3（分层红线：工具实现纯函数）。
"""

from office_agent_core.registry import register as _core_register

from .tools import register_all, specs

__all__ = ["_core_register", "register_all", "specs"]
