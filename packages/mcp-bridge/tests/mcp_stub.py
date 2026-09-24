"""mcp-bridge 单测的假 MCP Server（JSON-RPC over MockTransport，全程不出网）

独立成模块（不叫 conftest）：跨包全量跑测试时 ``conftest`` 会与其它包的 conftest 撞名，
本模块名唯一，故各测试模块直接 ``from mcp_stub import ...``。

断言目标是「MCP 协议契约 + 与内核工具链的同形性」，不是实现细节：
握手一次、方法名与参数形状、出站头齐备、溯源实测、错误分级与 HTTP 网关一致。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from office_agent_mcp_bridge import MCPProvider, protocol

ENDPOINT = "http://mcp.test/mcp"

#: 假 MCP Server 的工具清单：一条声明只读、一条未声明（未知即从严）
DEFAULT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "order.query",
        "description": "查询记录状态",
        "inputSchema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "title": "编号"}},
            "required": ["order_id"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "ticket.create",
        "description": "创建协同工单",
        "inputSchema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "title": "工单类型"}},
            "required": ["kind"],
        },
    },
]


class StubMCPServer:
    """内存假 MCP Server：按 JSON-RPC 方法分发，记录收到的请求，可注入各类故障。"""

    def __init__(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_result: dict[str, Any] | None = None,
        rpc_error: dict[str, Any] | None = None,
        http_status: int = 200,
        protocol_version: str = protocol.PROTOCOL_VERSION,
        sse: bool = False,
        timeout: bool = False,
    ) -> None:
        self.tools = DEFAULT_TOOLS if tools is None else tools
        self.tool_result = (
            tool_result
            if tool_result is not None
            else {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"ok": True}}
        )
        self.rpc_error = rpc_error
        self.http_status = http_status
        self.protocol_version = protocol_version
        self.sse = sse
        self.timeout = timeout
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def methods(self) -> list[str]:
        """收到过的方法名序列（断言握手只发生一次）。"""
        return [str(item.get("method")) for item in self.requests]

    def _result_for(self, method: str) -> dict[str, Any]:
        if method == protocol.METHOD_INITIALIZE:
            return {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stub", "version": "0.0.1"},
            }
        if method == protocol.METHOD_TOOLS_LIST:
            return {"tools": self.tools}
        if method == protocol.METHOD_TOOLS_CALL:
            return self.tool_result
        return {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.timeout:
            raise httpx.ReadTimeout("stub timeout", request=request)
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append({key.lower(): value for key, value in request.headers.items()})
        method = str(body.get("method"))
        if method == protocol.METHOD_INITIALIZED:
            return httpx.Response(202, text="")
        if self.http_status >= 400:
            return httpx.Response(self.http_status, json={"jsonrpc": "2.0", "id": 1})
        if self.rpc_error is not None and method != protocol.METHOD_INITIALIZE:
            payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": self.rpc_error,
            }
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": self._result_for(method),
            }
        if self.sse:
            text = f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=payload)

    def transport(self) -> httpx.MockTransport:
        """MockTransport 形态的传输层（注入 provider 后永不出网）。"""
        return httpx.MockTransport(self)


def make_provider(
    server: StubMCPServer, *, provider_id: str = "up", token: str = "tok"
) -> MCPProvider:
    """按假 Server 造一个 provider（客户端注入 MockTransport，连接由本类持有）。"""
    return MCPProvider(
        provider_id=provider_id,
        url=ENDPOINT,
        token=token,
        client=httpx.AsyncClient(transport=server.transport()),
    )
