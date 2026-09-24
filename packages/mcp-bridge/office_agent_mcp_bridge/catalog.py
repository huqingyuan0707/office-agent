"""MCP 工具清单 → ToolSpec 映射与自动注册（ToolSpec 与 MCP tool 天然同构的兑现处）

链路：宿主启动 → bootstrap.configure_from_settings() 建 MCPProvider
      → register_provider_tools(provider_id) → provider.list_tools()
      → spec_from_mcp_tool() 逐条映射 → registry.register() 进注册中心
      → 之后工具清单/鉴权/审批/审计/溯源全走内核既有链路，与本地工具无差别。

映射口径（**未知即从严**，写动作的唯一放行口仍是审批中心）：
- MCP 的 ``readOnlyHint=true`` → 只读 Scope、免审批、幂等；
- 注解缺失或非只读 → 写 Scope + **恒送审**（对端没声明只读，就不能替它担保只读）；
- 幂等性取 ``idempotentHint``，缺省随只读判定（只读天然幂等，写动作缺省不自动重试）。
"""

from __future__ import annotations

import logging
from typing import Any

from office_agent_core import registry
from office_agent_core.contracts import RemoteBinding, ToolSpec
from office_agent_mcp_bridge import protocol
from office_agent_mcp_bridge.provider import MCPProvider

logger = logging.getLogger(__name__)

#: 默认 Scope 命名（MCP 工具不带 Scope，由本桥按只读与否分配；可在调用处覆盖）
SCOPE_READ_DEFAULT = "mcp:read"
SCOPE_WRITE_DEFAULT = "mcp:write"


def _annotations(tool: dict[str, Any]) -> dict[str, Any]:
    """取工具注解对象（缺失/形状不符给空对象：注解是可选字段）。"""
    value = tool.get("annotations")
    return value if isinstance(value, dict) else {}


def spec_from_mcp_tool(
    provider_id: str,
    tool: dict[str, Any],
    *,
    scope_read: str = SCOPE_READ_DEFAULT,
    scope_write: str = SCOPE_WRITE_DEFAULT,
    name_prefix: str = "",
) -> ToolSpec:
    """把一条 MCP tool 定义映射成内核 ToolSpec（入参 Schema 直接沿用对端的 inputSchema）。"""
    raw_name = str(tool.get("name") or "").strip()
    if not raw_name:
        raise ValueError(f"MCP 提供方 {provider_id} 返回了无名工具，无法注册")
    name = f"{name_prefix}{raw_name}"
    annotations = _annotations(tool)
    read_only = annotations.get("readOnlyHint") is True
    idempotent = bool(annotations.get("idempotentHint", read_only))
    schema = tool.get("inputSchema")
    params = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
    return ToolSpec(
        name=name,
        scope=scope_read if read_only else scope_write,
        description=str(tool.get("description") or f"MCP 工具 {raw_name}（提供方 {provider_id}）"),
        params=params,
        remote=RemoteBinding(provider_id=provider_id, tool=raw_name),
        idempotent=idempotent,
        requires_approval=not read_only,
        approval_action=name if not read_only else "",
    )


async def register_provider_tools(
    provider: MCPProvider,
    *,
    scope_read: str = SCOPE_READ_DEFAULT,
    scope_write: str = SCOPE_WRITE_DEFAULT,
    only: tuple[str, ...] = (),
    name_prefix: str = "",
) -> list[str]:
    """发现并注册该提供方的工具；返回已注册工具名（``only`` 非空时只注册名单内的）。"""
    tools = await provider.list_tools()
    registered: list[str] = []
    for tool in tools:
        spec = spec_from_mcp_tool(
            provider.provider_id,
            tool,
            scope_read=scope_read,
            scope_write=scope_write,
            name_prefix=name_prefix,
        )
        if only and spec.name not in only:
            continue
        registry.register(spec)
        registered.append(spec.name)
    logger.info(
        "MCP 提供方 %s 工具已注册 %d 个：%s",
        provider.provider_id,
        len(registered),
        "、".join(registered) or "（无）",
    )
    return registered


def describe_protocol() -> dict[str, str]:
    """协议自述（治理状态页/排障用；不含任何凭据）。"""
    return {"protocol_version": protocol.PROTOCOL_VERSION, "transport": "http+json-rpc"}
