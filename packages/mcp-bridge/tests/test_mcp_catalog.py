"""工具发现与注册规则单测：只读免审、未知即从严、装配降级不阻断

断言的是「MCP 注解 → 内核治理属性」的映射口径（写动作唯一放行口仍是审批中心），
以及装配期对端不可用时的降级行为（宿主照常起，工具自然缺席）。
"""

from __future__ import annotations

import pytest
from mcp_stub import StubMCPServer, make_provider

from office_agent_core import linkage, registry
from office_agent_core.settings import ProviderConfig
from office_agent_mcp_bridge import (
    SCOPE_READ_DEFAULT,
    SCOPE_WRITE_DEFAULT,
    MCPProvider,
    bootstrap,
    catalog,
)

_DUMMY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


@pytest.fixture(autouse=True)
def _clean_registry():
    """注册中心是进程内单例：用例前后清空，防工具清单串味。"""
    registry.reset()
    yield
    registry.reset()


def test_read_only_tool_maps_to_read_scope_without_approval():
    """声明只读 → 只读 Scope、免审批、幂等。"""
    spec = catalog.spec_from_mcp_tool(
        "up",
        {
            "name": "order.query",
            "description": "查询记录",
            "inputSchema": _DUMMY_SCHEMA,
            "annotations": {"readOnlyHint": True},
        },
    )

    assert spec.scope == SCOPE_READ_DEFAULT
    assert spec.requires_approval is False
    assert spec.idempotent is True
    assert spec.approval_action == ""
    assert spec.remote is not None and spec.remote.tool == "order.query"


def test_unannotated_tool_requires_approval():
    """注解缺失（对端没声明只读）→ 写 Scope + 恒送审 + 缺省不自动重试。"""
    spec = catalog.spec_from_mcp_tool("up", {"name": "ticket.create", "inputSchema": _DUMMY_SCHEMA})

    assert spec.scope == SCOPE_WRITE_DEFAULT
    assert spec.requires_approval is True
    assert spec.approval_action == "ticket.create"
    assert spec.idempotent is False


def test_explicit_read_only_false_requires_approval():
    """显式 readOnlyHint=false 同样恒送审。"""
    spec = catalog.spec_from_mcp_tool(
        "up",
        {
            "name": "ticket.create",
            "inputSchema": _DUMMY_SCHEMA,
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
    )

    assert spec.requires_approval is True
    assert spec.idempotent is True


def test_missing_schema_and_description_fall_back_safely():
    """对端没给 Schema/描述时给最小可用占位，不让注册失败（参数校验仍由内核兜底）。"""
    spec = catalog.spec_from_mcp_tool("up", {"name": "ping"})

    assert spec.params == {"type": "object", "properties": {}}
    assert "ping" in spec.description


def test_nameless_tool_is_rejected():
    """无名工具无法注册：宁可报错，也不生成一个调用不了的工具。"""
    with pytest.raises(ValueError):
        catalog.spec_from_mcp_tool("up", {"description": "无名"})


async def test_register_provider_tools_with_filter_and_prefix():
    """``only`` 过滤 + ``name_prefix`` 生效，且注册结果进内核注册中心。"""
    server = StubMCPServer()
    provider = make_provider(server)

    names = await catalog.register_provider_tools(
        provider, only=("up.order.query",), name_prefix="up."
    )

    assert names == ["up.order.query"]
    spec = registry.get("up.order.query")
    assert spec.is_remote and spec.remote is not None
    assert spec.remote.provider_id == "up"
    assert spec.scope == SCOPE_READ_DEFAULT


def test_configure_from_settings_claims_only_mcp_transport():
    """协议分流：只认领 transport=mcp 的条目，http 条目留给内核。"""
    providers = {
        "gw": ProviderConfig(base_url="http://gw.test"),
        "mcp-up": ProviderConfig(base_url="http://mcp.test", path="/mcp", transport="mcp"),
    }

    ids = bootstrap.configure_from_settings(providers)

    assert ids == ["mcp-up"]
    claimed = linkage.get_provider("mcp-up")
    assert isinstance(claimed, MCPProvider)
    assert claimed.endpoint == "http://mcp.test/mcp"
    assert linkage.get_provider("gw") is None


async def test_register_all_tools_degrades_without_raising():
    """对端不可用时只记录错误、不抛出：宿主照常启动，该提供方工具自然缺席。"""
    providers = {
        "mcp-up": ProviderConfig(base_url="http://mcp.test", path="/mcp", transport="mcp"),
    }
    # 注入「HTTP 500」的客户端：握手即失败，模拟对端没起/网关故障
    linkage.register_provider(make_provider(StubMCPServer(http_status=500), provider_id="mcp-up"))

    outcome = await bootstrap.register_all_tools(providers=providers)

    assert outcome["providers"] == ["mcp-up"]
    assert outcome["tools"] == []
    assert "mcp-up" in outcome["errors"]


async def test_register_all_tools_registers_discovered_tools():
    """对端可用时：工具清单全量进注册中心（只读免审、未注解恒送审）。"""
    providers = {
        "mcp-up": ProviderConfig(base_url="http://mcp.test", path="/mcp", transport="mcp"),
    }
    linkage.register_provider(make_provider(StubMCPServer(), provider_id="mcp-up"))

    outcome = await bootstrap.register_all_tools(providers=providers)

    assert outcome["errors"] == {}
    assert outcome["tools"] == ["order.query", "ticket.create"]
    assert registry.get("order.query").requires_approval is False
    assert registry.get("ticket.create").requires_approval is True
