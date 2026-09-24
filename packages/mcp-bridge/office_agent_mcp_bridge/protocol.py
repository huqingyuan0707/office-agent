"""MCP wire 协议编解码（JSON-RPC 2.0 over HTTP；零三方 SDK 依赖）

链路：MCPProvider 组装请求 → 本模块编码 → httpx 出站 → 本模块解析响应
      → 结果交回 provider 按 linkage.envelope 的统一口径拆信封。

口径：
- 协议版本与客户端信息在此**钉死唯一出处**（provider 不各自硬编码字符串）；
- 传输层故障（连不通 / 超时 / HTTP 5xx / 非 JSON / 响应不合 JSON-RPC / JSON-RPC error）
  一律归 UpstreamError（依赖故障，可重试、计熔断）——「业务拒绝」的判定权在 envelope 层，
  本模块不越权翻译，避免同一份上游语义在两处各说各话；
- 服务端可能以 ``text/event-stream``（SSE）回包（streamable HTTP 传输），故解析时
  先按 content-type 取最后一个事件的 data 载荷，再当 JSON 处理。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from office_agent_core.errors import UpstreamError

#: MCP 协议版本（本桥实现的唯一出处；initialize 回执版本不符只告警不阻断）
PROTOCOL_VERSION = "2025-06-18"

METHOD_INITIALIZE = "initialize"
METHOD_INITIALIZED = "notifications/initialized"
METHOD_TOOLS_LIST = "tools/list"
METHOD_TOOLS_CALL = "tools/call"

#: 客户端信息（对端日志/治理据此识别调用方）
CLIENT_INFO: dict[str, str] = {"name": "office-agent-mcp-bridge", "version": "0.1.0"}


def request_payload(
    method: str, params: dict[str, Any] | None, *, request_id: int
) -> dict[str, Any]:
    """组装 JSON-RPC 2.0 请求（params 为空则省略该键，符合协议惯例）。"""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def notification_payload(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """组装 JSON-RPC 2.0 通知（无 id，对端不回包）。"""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def _sse_data(text: str) -> str:
    """从 SSE 报文里取**最后一个事件**的 data 载荷（多行 data 按协议拼接）。"""
    last: list[str] = []
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            if current:
                last = current
                current = []
            continue
        if line.startswith("data:"):
            current.append(line[len("data:") :].lstrip())
    if current:
        last = current
    return "\n".join(last)


def _body_text(response: httpx.Response) -> str:
    """取响应正文文本：SSE 先抽 data 载荷，其余原样。"""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        return _sse_data(response.text)
    return response.text


def parse_result(response: httpx.Response, *, provider_id: str, method: str) -> dict[str, Any]:
    """解析 JSON-RPC 响应，返回 ``result`` 对象；任何不合契约之处都按依赖故障处理。

    HTTP 4xx/5xx 一并归依赖故障：MCP 的 HTTP 状态码不承载业务语义，
    业务拒绝由工具结果（isError / 信封 code）表达，混用会让熔断把正常业务结果算成故障。
    """
    if response.status_code >= 400:
        raise UpstreamError(
            f"MCP 提供方 {provider_id} 调用 {method} 失败（HTTP {response.status_code}）"
        )
    try:
        payload = json.loads(_body_text(response))
    except (json.JSONDecodeError, ValueError) as exc:
        raise UpstreamError(
            f"MCP 提供方 {provider_id} 的 {method} 返回非 JSON（HTTP {response.status_code}）"
        ) from exc
    if not isinstance(payload, dict) or "jsonrpc" not in payload:
        raise UpstreamError(f"MCP 提供方 {provider_id} 的 {method} 响应不符合 JSON-RPC 契约")
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code", -1)
        message = str(error.get("message") or "")
        raise UpstreamError(
            f"MCP 提供方 {provider_id} 的 {method} 返回错误（code={code}）：{message}"
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise UpstreamError(f"MCP 提供方 {provider_id} 的 {method} 未返回对象结果")
    return result


def result_text(result: dict[str, Any]) -> str:
    """汇总工具结果里的文本内容（``content`` 中 type=text 的项）。"""
    items = result.get("content")
    if not isinstance(items, list):
        return ""
    parts = [
        str(item.get("text"))
        for item in items
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text") is not None
    ]
    return "\n".join(parts)


def try_json_object(text: str) -> dict[str, Any] | None:
    """尽力把文本解析成 JSON 对象；不是对象一律给 None（不臆造结构）。"""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
