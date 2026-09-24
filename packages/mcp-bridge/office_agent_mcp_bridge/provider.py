"""MCP 工具提供方客户端（与 linkage.RemoteToolProvider **同形**：provider_id + invoke + aclose）

链路：executor 发现 spec.remote 非空 → linkage.provider_of(provider_id) 取本类实例
      → invoke(binding, args) → initialize 握手（惰性一次）→ tools/call
      → 按 linkage.envelope 统一口径拆信封 → 返回 (data, provenance)。

同形红线（方案 §7.4「换协议不改业务代码」）：本类实现与 HTTP 网关客户端**同一接口**，
故经 linkage.register_provider() 登记后，registry/executor/审批/审计/溯源链路零改动——
对端从自定义网关换成标准 MCP Server，本侧只改一行配置（transport 由 http 改 mcp）。

出站头与 HTTP 网关保持一致（跨系统可追溯的最小集合）：Authorization / X-Trace-Id /
X-On-Behalf-Of / X-Office-Agent，对端按同一套规则识别调用方与真实发起人。
"""

from __future__ import annotations

import itertools
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from office_agent_core.contracts import Provenance, RemoteBinding
from office_agent_core.errors import BusinessError, ErrorCode, UpstreamError
from office_agent_core.linkage.envelope import looks_like_envelope, unwrap_envelope
from office_agent_core.observability import record
from office_agent_core.settings import ProviderConfig, settings
from office_agent_mcp_bridge import protocol

logger = logging.getLogger(__name__)

#: 内核自定义网关的默认路径：MCP 提供方忘了显式配 path 时给一条可照做的告警
_GATEWAY_DEFAULT_PATH = "/api/v1/agent-gateway/invoke"

#: 协议标识（溯源里透出，便于与 HTTP 网关的溯源区分）
TRANSPORT_MCP = "mcp"


class MCPProvider:
    """一个 MCP Server 的客户端（provider_id 唯一；凭据只在内存，绝不落库落日志）。"""

    def __init__(
        self,
        *,
        provider_id: str,
        url: str,
        token: str = "",
        timeout_seconds: float = 10.0,
        protocol_version: str = protocol.PROTOCOL_VERSION,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not provider_id.strip():
            raise ValueError("provider_id 不能为空")
        if not url.strip():
            raise ValueError(f"MCP 提供方 {provider_id} 缺少端点地址")
        self.provider_id = provider_id.strip()
        self._url = url.strip()
        self._token = token
        self._timeout = float(timeout_seconds)
        self._protocol_version = protocol_version
        self._client = client
        self._owns_client = client is None
        self._ids = itertools.count(1)
        self._initialized = False

    @classmethod
    def from_config(cls, provider_id: str, config: ProviderConfig) -> MCPProvider:
        """按 Settings 的提供方配置构建（base_url + path 拼 MCP 端点，token 用后即弃）。"""
        url = f"{config.base_url.strip().rstrip('/')}{config.path}"
        if config.path == _GATEWAY_DEFAULT_PATH:
            logger.warning(
                "MCP 提供方 %s 未显式配置 path，正按内核网关默认路径 %s 尝试；"
                "标准 MCP Server 的端点通常形如 /mcp，请在 LINKAGE_PROVIDERS 里补上 path",
                provider_id,
                config.path,
            )
        return cls(
            provider_id=provider_id,
            url=url,
            token=config.token.get_secret_value(),
            timeout_seconds=config.timeout_seconds,
        )

    @property
    def endpoint(self) -> str:
        """出站端点（溯源用；不含凭据，可安全落审计）。"""
        return self._url

    # ---------------- 客户端生命周期 ----------------

    def _ensure_client(self) -> httpx.AsyncClient:
        """惰性建共享 AsyncClient（复用连接池；测试可注入 MockTransport 客户端）。"""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """关闭自有客户端（注入的客户端由调用方负责关闭）。"""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ---------------- 协议交互 ----------------

    def _headers(self, *, trace_id: str, on_behalf_of: str) -> dict[str, str]:
        """出站头（与 HTTP 网关同口径：trace / 身份 / 调用方 / 授权）。"""
        headers = {
            # streamable HTTP 传输：对端可能回 SSE 或普通 JSON，两种都要接受
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "X-Trace-Id": trace_id,
            "X-Office-Agent": f"{settings.LINKAGE_CLIENT_NAME}/{settings.APP_VERSION}",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if on_behalf_of:
            headers["X-On-Behalf-Of"] = on_behalf_of
        return headers

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        trace_id: str,
        on_behalf_of: str,
    ) -> dict[str, Any]:
        """发一次 JSON-RPC 请求并解析 result（超时/连不通/契约不符都按依赖故障抛）。"""
        client = self._ensure_client()
        payload = protocol.request_payload(method, params, request_id=next(self._ids))
        try:
            response = await client.post(
                self.endpoint,
                json=payload,
                headers=self._headers(trace_id=trace_id, on_behalf_of=on_behalf_of),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"MCP 提供方 {self.provider_id} 调用 {method} 超时（>{self._timeout}s）"
            ) from exc
        except (httpx.HTTPError, OSError) as exc:
            raise UpstreamError(
                f"MCP 提供方 {self.provider_id} 不可达（{type(exc).__name__}）"
            ) from exc
        return protocol.parse_result(response, provider_id=self.provider_id, method=method)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """发通知（无回执）：失败只告警——通知不是业务结果，不该把调用链打断。"""
        client = self._ensure_client()
        try:
            await client.post(
                self.endpoint,
                json=protocol.notification_payload(method, params),
                headers=self._headers(trace_id="", on_behalf_of=""),
                timeout=self._timeout,
            )
        except Exception as exc:
            logger.warning("MCP 通知 %s 发送失败（忽略）：%s", method, str(exc)[:200])

    async def initialize(self) -> dict[str, Any]:
        """惰性握手（每提供方一次）：协商协议版本并声明客户端能力。"""
        if self._initialized:
            return {"protocol_version": self._protocol_version, "cached": True}
        result = await self._rpc(
            protocol.METHOD_INITIALIZE,
            {
                "protocolVersion": self._protocol_version,
                "capabilities": {},
                "clientInfo": dict(protocol.CLIENT_INFO),
            },
            trace_id="",
            on_behalf_of="",
        )
        agreed = str(result.get("protocolVersion") or "")
        if agreed and agreed != self._protocol_version:
            logger.warning(
                "MCP 提供方 %s 协商到协议版本 %s（本桥实现为 %s），按对端版本继续",
                self.provider_id,
                agreed,
                self._protocol_version,
            )
            self._protocol_version = agreed
        self._initialized = True
        await self._notify(protocol.METHOD_INITIALIZED)
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        """发现对端工具清单（catalog 据此映射 ToolSpec）。"""
        await self.initialize()
        result = await self._rpc(protocol.METHOD_TOOLS_LIST, {}, trace_id="", on_behalf_of="")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise UpstreamError(f"MCP 提供方 {self.provider_id} 的 tools/list 未返回工具数组")
        return [tool for tool in tools if isinstance(tool, dict)]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, trace_id: str, on_behalf_of: str
    ) -> dict[str, Any]:
        """调用对端工具（tools/call），返回原始 result（拆信封由 invoke 负责）。"""
        await self.initialize()
        return await self._rpc(
            protocol.METHOD_TOOLS_CALL,
            {"name": name, "arguments": arguments},
            trace_id=trace_id,
            on_behalf_of=on_behalf_of,
        )

    # ---------------- 与 HTTP 网关同形的 invoke ----------------

    async def invoke(
        self,
        binding: RemoteBinding,
        args: dict[str, Any],
        *,
        trace_id: str,
        on_behalf_of: str = "",
    ) -> tuple[dict[str, Any], Provenance]:
        """调用对端 MCP 工具，返回 ``(data, provenance)``（接口与 HTTP 网关客户端一致）。"""
        started = time.perf_counter()
        try:
            result = await self.call_tool(
                binding.tool, args, trace_id=trace_id, on_behalf_of=on_behalf_of
            )
        except (BusinessError, UpstreamError, TimeoutError):
            self._trace(binding, started, ok=False, trace_id=trace_id)
            raise
        # 溯源时刻取「收到响应」的实测时间，不是请求发起时间
        fetched_at = datetime.now(UTC).isoformat()
        self._trace(binding, started, ok=True, trace_id=trace_id)
        return self._unwrap(result, binding, fetched_at=fetched_at)

    def _unwrap(
        self, result: dict[str, Any], binding: RemoteBinding, *, fetched_at: str
    ) -> tuple[dict[str, Any], Provenance]:
        """把 MCP 工具结果折成 ``(data, provenance)``。

        分流规则（三种对端形态都能接）：
        1. ``isError`` → 工具自报失败：文本里若是信封则按号段分级，否则给 4008 业务拒绝；
        2. ``structuredContent`` / 文本 JSON 里带 ``code`` → 按上游信封统一口径拆解；
        3. 裸对象 → 直接作为 data（第三方 MCP Server 的常见形态），溯源仍由本层实测填写。
        """
        extra = {"transport": TRANSPORT_MCP, "protocol_version": self._protocol_version}
        if result.get("isError"):
            text = protocol.result_text(result) or "上游 MCP 工具未给出失败详情"
            payload = protocol.try_json_object(text)
            if payload is not None and looks_like_envelope(payload):
                return unwrap_envelope(
                    payload,
                    provider_id=self.provider_id,
                    tool=binding.tool,
                    source_endpoint=f"POST {self.endpoint}",
                    fetched_at=fetched_at,
                    extra=extra,
                )
            raise BusinessError(
                ErrorCode.TOOL_CALL_FAILED,
                f"MCP 工具 {binding.tool} 执行失败：{text[:200]}",
            )

        payload = self._payload_of(result, binding)
        if looks_like_envelope(payload):
            return unwrap_envelope(
                payload,
                provider_id=self.provider_id,
                tool=binding.tool,
                source_endpoint=f"POST {self.endpoint}",
                fetched_at=fetched_at,
                extra=extra,
            )
        # 裸对象：包一层最小信封复用同一份溯源口径（不复制字段定义）
        return unwrap_envelope(
            {"code": 0, "data": payload},
            provider_id=self.provider_id,
            tool=binding.tool,
            source_endpoint=f"POST {self.endpoint}",
            fetched_at=fetched_at,
            extra=extra,
        )

    def _payload_of(self, result: dict[str, Any], binding: RemoteBinding) -> dict[str, Any]:
        """从工具结果里取出载荷对象（结构化内容优先，其次文本 JSON，再次纯文本）。"""
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        text = protocol.result_text(result)
        if not text:
            raise UpstreamError(f"MCP 提供方 {self.provider_id} 的工具 {binding.tool} 返回了空内容")
        parsed = protocol.try_json_object(text)
        return parsed if parsed is not None else {"text": text}

    def _trace(self, binding: RemoteBinding, started: float, *, ok: bool, trace_id: str) -> None:
        """联动链路可观测（与 HTTP 网关同事件名，便于统一算上游可用率）。"""
        record(
            "agent.linkage",
            {
                "provider": self.provider_id,
                "tool": binding.tool,
                "transport": TRANSPORT_MCP,
                "ok": ok,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "trace_id": trace_id,
            },
        )
