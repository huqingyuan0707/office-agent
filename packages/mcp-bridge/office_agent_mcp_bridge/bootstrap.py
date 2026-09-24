"""MCP 桥装配入口（宿主启动时按 Settings 认领 transport="mcp" 的提供方并注册其工具）

链路：server lifespan → configure_from_settings()（建 MCPProvider 进 linkage 注册表）
      → register_all_tools()（逐个 tools/list 自动注册 ToolSpec）→ 关停时 shutdown()。

红线：
- 只认领 ``transport == "mcp"`` 的条目，其余（内核自带 http 网关）不碰——协议分流唯一在此；
- 凭据只来自环境变量（Settings.LINKAGE_PROVIDERS），本包既不写地址也不写令牌；
- **降级不阻断启动**：某个 MCP Server 没起/握手失败，只记错误不注册其工具，
  宿主照常起（未注册的工具在清单里自然缺席，绝不注册一个注定调不通的工具）。
"""

from __future__ import annotations

import logging
from typing import Any

from office_agent_core import linkage
from office_agent_core.settings import ProviderConfig, settings
from office_agent_mcp_bridge.catalog import (
    SCOPE_READ_DEFAULT,
    SCOPE_WRITE_DEFAULT,
    describe_protocol,
    register_provider_tools,
    spec_from_mcp_tool,
)
from office_agent_mcp_bridge.provider import TRANSPORT_MCP, MCPProvider

logger = logging.getLogger(__name__)


def mcp_provider_ids(providers: dict[str, ProviderConfig] | None = None) -> list[str]:
    """Settings 里声明为 MCP 传输的 provider_id（稳定排序，便于启动日志与断言）。"""
    source = settings.LINKAGE_PROVIDERS if providers is None else providers
    return sorted(pid for pid, config in source.items() if config.transport == TRANSPORT_MCP)


def configure_from_settings(providers: dict[str, ProviderConfig] | None = None) -> list[str]:
    """按 Settings 建好全部 MCP 提供方客户端，返回已登记的 provider_id 列表。"""
    source = settings.LINKAGE_PROVIDERS if providers is None else providers
    ids: list[str] = []
    for provider_id in mcp_provider_ids(source):
        linkage.register_provider(MCPProvider.from_config(provider_id, source[provider_id]))
        ids.append(provider_id)
    return ids


async def register_all_tools(
    *,
    scope_read: str = SCOPE_READ_DEFAULT,
    scope_write: str = SCOPE_WRITE_DEFAULT,
    providers: dict[str, ProviderConfig] | None = None,
) -> dict[str, Any]:
    """逐个 MCP 提供方发现并注册工具；单个失败只记录（降级不阻断启动）。

    返回 ``{"providers": [...], "tools": [...], "errors": {provider_id: 原因}}``。
    """
    tools: list[str] = []
    errors: dict[str, str] = {}
    ids = mcp_provider_ids(providers)
    for provider_id in ids:
        provider = linkage.get_provider(provider_id)
        if not isinstance(provider, MCPProvider):
            errors[provider_id] = "提供方未登记为 MCP 客户端，请检查启动装配顺序"
            continue
        try:
            tools.extend(
                await register_provider_tools(
                    provider, scope_read=scope_read, scope_write=scope_write
                )
            )
        except Exception as exc:
            errors[provider_id] = str(exc)[:200]
            logger.warning(
                "MCP 提供方 %s 工具发现失败（该提供方工具本次缺席）：%s", provider_id, exc
            )
    return {"providers": ids, "tools": tools, "errors": errors}


async def shutdown() -> None:
    """关停本桥登记的 MCP 客户端（幂等；未登记时什么也不做）。"""
    for provider_id in mcp_provider_ids():
        provider = linkage.get_provider(provider_id)
        if isinstance(provider, MCPProvider):
            await provider.aclose()


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
