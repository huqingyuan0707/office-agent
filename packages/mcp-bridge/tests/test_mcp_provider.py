"""MCPProvider 单测：协议往返 / 出站头 / 溯源实测 / 错误分级（与 HTTP 网关同口径）

每个用例都通过 ``linkage.provider_of`` 之外的直连方式断言 MCP 层契约，
再用一条用例证明「登记进 linkage 后与 HTTP 网关客户端同形可互换」。
"""

from __future__ import annotations

import json

import pytest
from mcp_stub import ENDPOINT, StubMCPServer, make_provider

from office_agent_core import linkage
from office_agent_core.contracts import RemoteBinding
from office_agent_core.errors import BusinessError, UpstreamError
from office_agent_mcp_bridge import protocol

_BINDING = RemoteBinding(provider_id="up", tool="order.query")


async def test_invoke_roundtrip_structured_content():
    """结构化内容直接作为 data；溯源由本层实测填写（协议标识一并透出）。"""
    server = StubMCPServer()
    provider = make_provider(server)

    data, provenance = await provider.invoke(
        _BINDING, {"order_id": "A1"}, trace_id="tr-1", on_behalf_of="alice"
    )

    assert data == {"ok": True}
    assert provenance["provider_id"] == "up"
    assert provenance["source_endpoint"] == f"POST {ENDPOINT}"
    assert provenance["tool"] == "order.query"
    assert provenance["fetched_at"]
    assert provenance["transport"] == "mcp"
    assert provenance["protocol_version"] == protocol.PROTOCOL_VERSION
    # 请求形状：先握手、再通知已初始化、最后 tools/call 带 name + arguments
    assert server.methods() == [
        protocol.METHOD_INITIALIZE,
        protocol.METHOD_INITIALIZED,
        protocol.METHOD_TOOLS_CALL,
    ]
    call = server.requests[-1]
    assert call["params"] == {"name": "order.query", "arguments": {"order_id": "A1"}}
    # 出站头与 HTTP 网关同口径（trace / 身份 / 调用方 / 授权）
    headers = server.headers[-1]
    assert headers["x-trace-id"] == "tr-1"
    assert headers["x-on-behalf-of"] == "alice"
    assert headers["authorization"] == "Bearer tok"
    assert headers["x-office-agent"].startswith("office-agent/")


async def test_initialize_handshake_happens_once():
    """握手每提供方一次（惰性缓存）：连续两次调用只发一次 initialize。"""
    server = StubMCPServer()
    provider = make_provider(server)

    await provider.invoke(_BINDING, {}, trace_id="tr")
    await provider.invoke(_BINDING, {}, trace_id="tr")

    assert server.methods().count(protocol.METHOD_INITIALIZE) == 1


async def test_text_content_json_envelope_is_unwrapped():
    """文本内容里的上游信封按统一口径拆解（code=0 取 data + 上游 trace）。"""
    envelope = {"code": 0, "msg": "操作成功", "data": {"order_id": "A1"}, "trace_id": "up-9"}
    server = StubMCPServer(
        tool_result={"content": [{"type": "text", "text": json.dumps(envelope)}]}
    )
    provider = make_provider(server)

    data, provenance = await provider.invoke(_BINDING, {}, trace_id="tr")

    assert data == {"order_id": "A1"}
    assert provenance["upstream_trace_id"] == "up-9"


async def test_plain_text_content_is_wrapped_not_fabricated():
    """非 JSON 文本不臆造结构：原样放进 text 字段。"""
    server = StubMCPServer(tool_result={"content": [{"type": "text", "text": "纯文本结果"}]})
    provider = make_provider(server)

    data, _ = await provider.invoke(_BINDING, {}, trace_id="tr")

    assert data == {"text": "纯文本结果"}


async def test_business_code_is_passed_through():
    """上游域内业务码（4xxx）原码透传，本桥不做任何翻译。"""
    envelope = {"code": 4006, "msg": "权限不足", "data": None}
    server = StubMCPServer(
        tool_result={"content": [{"type": "text", "text": json.dumps(envelope)}]}
    )
    provider = make_provider(server)

    with pytest.raises(BusinessError) as excinfo:
        await provider.invoke(_BINDING, {}, trace_id="tr")
    assert excinfo.value.code == 4006
    assert excinfo.value.msg == "权限不足"


async def test_system_code_is_dependency_error():
    """上游 5xxx 是依赖故障（可重试、计熔断），绝不当前端「正常失败」。"""
    envelope = {"code": 5000, "msg": "内部错误", "data": None}
    server = StubMCPServer(
        tool_result={"content": [{"type": "text", "text": json.dumps(envelope)}]}
    )
    provider = make_provider(server)

    with pytest.raises(UpstreamError):
        await provider.invoke(_BINDING, {}, trace_id="tr")


async def test_is_error_without_envelope_is_business_rejection():
    """isError 且无信封 → 4008 业务拒绝（工具自报失败，不是依赖抖动）。"""
    server = StubMCPServer(
        tool_result={
            "isError": True,
            "content": [{"type": "text", "text": "参数 kind 不合法"}],
        }
    )
    provider = make_provider(server)

    with pytest.raises(BusinessError) as excinfo:
        await provider.invoke(_BINDING, {}, trace_id="tr")
    assert excinfo.value.code == 4008
    assert "参数 kind 不合法" in excinfo.value.msg


async def test_is_error_with_envelope_keeps_upstream_code():
    """isError 且带信封 → 仍按信封号段分级（对端错误码不被本桥改写）。"""
    envelope = {"code": 4004, "msg": "审批被驳回", "data": None}
    server = StubMCPServer(
        tool_result={"isError": True, "content": [{"type": "text", "text": json.dumps(envelope)}]}
    )
    provider = make_provider(server)

    with pytest.raises(BusinessError) as excinfo:
        await provider.invoke(_BINDING, {}, trace_id="tr")
    assert excinfo.value.code == 4004


async def test_jsonrpc_error_is_dependency_error():
    """JSON-RPC error（方法不存在等）属依赖故障，不重试到业务层。"""
    server = StubMCPServer(rpc_error={"code": -32601, "message": "Method not found"})
    provider = make_provider(server)

    with pytest.raises(UpstreamError) as excinfo:
        await provider.invoke(_BINDING, {}, trace_id="tr")
    assert "Method not found" in excinfo.value.msg


async def test_http_error_is_dependency_error():
    """HTTP 4xx/5xx 一律依赖故障：MCP 的 HTTP 状态码不承载业务语义。"""
    server = StubMCPServer(http_status=503)
    provider = make_provider(server)

    with pytest.raises(UpstreamError):
        await provider.invoke(_BINDING, {}, trace_id="tr")


async def test_timeout_raises_timeout_error():
    """超时抛 TimeoutError（executor 据此给 4002，与 HTTP 网关同一分级）。"""
    server = StubMCPServer(timeout=True)
    provider = make_provider(server)

    with pytest.raises(TimeoutError):
        await provider.invoke(_BINDING, {}, trace_id="tr")


async def test_sse_response_is_parsed():
    """streamable HTTP 的 SSE 回包也能解析（取最后一个事件的 data 载荷）。"""
    server = StubMCPServer(sse=True)
    provider = make_provider(server)

    data, _ = await provider.invoke(_BINDING, {}, trace_id="tr")

    assert data == {"ok": True}


async def test_empty_content_is_dependency_error():
    """空内容不编造数据，按依赖故障抛错。"""
    server = StubMCPServer(tool_result={"content": []})
    provider = make_provider(server)

    with pytest.raises(UpstreamError):
        await provider.invoke(_BINDING, {}, trace_id="tr")


async def test_registered_provider_is_interchangeable_with_http_gateway():
    """登记进 linkage 后与 HTTP 网关客户端同形：provider_of 取到即可直接 invoke。"""
    server = StubMCPServer()
    linkage.register_provider(make_provider(server))

    provider = linkage.provider_of("up")
    data, provenance = await provider.invoke(_BINDING, {}, trace_id="tr")

    assert data == {"ok": True}
    assert provenance["provider_id"] == "up"
