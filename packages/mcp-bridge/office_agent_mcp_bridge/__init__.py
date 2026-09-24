"""office-agent MCP 协议桥（M3：对端升级为标准 MCP Server 后本侧的唯一接入点）

定位（决策见 ``docs/ADR-0004-MCP协议桥与IM审批通知出站.md``）：
- 本包是 MCP **客户端**适配器——office-agent 经它消费外部 MCP Server，而非对外暴露工具；
- ``MCPProvider`` 与内核 HTTP 网关客户端**同形**（provider_id / invoke / aclose），
  经 ``linkage.register_provider()`` 登记后 executor 与整条工具链零改动；
- 工具清单由 ``tools/list`` 自动发现并映射为 ToolSpec（只读免审、非只读恒送审）。

典型用法（宿主 lifespan）::

    from office_agent_mcp_bridge import configure_from_settings, register_all_tools

    configure_from_settings()          # 认领 transport="mcp" 的提供方
    await register_all_tools()         # 发现并注册其工具（失败降级不阻断启动）
"""

from office_agent_mcp_bridge.bootstrap import (
    SCOPE_READ_DEFAULT,
    SCOPE_WRITE_DEFAULT,
    TRANSPORT_MCP,
    MCPProvider,
    configure_from_settings,
    describe_protocol,
    mcp_provider_ids,
    register_all_tools,
    register_provider_tools,
    shutdown,
    spec_from_mcp_tool,
)

__all__ = [
    "SCOPE_READ_DEFAULT",
    "SCOPE_WRITE_DEFAULT",
    "TRANSPORT_MCP",
    "MCPProvider",
    "configure_from_settings",
    "describe_protocol",
    "mcp_provider_ids",
    "register_all_tools",
    "register_provider_tools",
    "shutdown",
    "spec_from_mcp_tool",
]
