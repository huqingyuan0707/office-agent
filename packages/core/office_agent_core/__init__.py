"""office-agent 内核公开 API

领域无关：本包不出现任何具体业务域的名词与分支；业务域能力一律以插件形式在
``plugins/`` 中声明（本地实现或远程绑定），内核只提供注册/鉴权/审批/执行/审计/联动六件事。

典型用法（宿主）::

    from office_agent_core import executor, registry
    from office_agent_core.contracts import ToolContext

    registry.register(spec)
    data = await executor.call(ctx, name="<工具名>", args={...}, trace_id="<trace>")

各子模块按需导入（``office_agent_core.executor`` / ``.registry`` / ``.policy`` /
``.audit`` / ``.observability`` / ``.linkage``），此处只做契约与配置的便捷重导出，
避免包初始化时把执行链上的全部依赖一次性拉起。
"""

from office_agent_core.contracts import (
    AgentState,
    RemoteBinding,
    ToolContext,
    ToolSpec,
    can_transition,
    ensure_transition,
    state_label,
    validate_args,
)
from office_agent_core.errors import BusinessError, ErrorCode, UpstreamError
from office_agent_core.settings import ProviderConfig, Settings, is_default_seed_password
from office_agent_core.settings import settings as settings

__all__ = [
    "AgentState",
    "BusinessError",
    "ErrorCode",
    "ProviderConfig",
    "RemoteBinding",
    "Settings",
    "ToolContext",
    "ToolSpec",
    "UpstreamError",
    "can_transition",
    "ensure_transition",
    "is_default_seed_password",
    "settings",
    "state_label",
    "validate_args",
]
