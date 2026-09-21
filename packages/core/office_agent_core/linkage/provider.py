"""远程工具提供方（内核出站 HTTP 客户端，跨系统联动的唯一出口）

链路：executor 发现 spec.remote 非空 → linkage.provider_of(provider_id)
      → provider.invoke(binding, args) → 出站注入头 → 解析上游信封
      → 返回 (data, provenance)；数据落地与展示由调用方负责。

出站头（跨系统可追溯的最小集合，逐条都有用途）：
- ``Authorization: Bearer <服务账号令牌>``：上游按普通登录用户对待本内核，不新造信任体系；
- ``X-Trace-Id``：本内核本次调用的 trace，上游原样记录 → 跨系统排障一条线；
- ``X-On-Behalf-Of``：真实发起人（上游据此人写审批/审计留痕，防止链断在系统边界）；
- ``X-Office-Agent``：调用方标识，上游可做治理与限流区分。

错误分级（与 executor 的重试/熔断口径配套）：
- 上游 1xxx/3xxx/4xxx（确定性业务结果）→ BusinessError **原码透传**，不重试、不计熔断；
- 上游 5xxx / 连不通 / 超时 / 响应不合契约 → UpstreamError（可重试、计熔断），
  绝不把上游的 5xx 当业务结果抛给前端当「正常失败」。
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from office_agent_core.contracts import Provenance, RemoteBinding
from office_agent_core.errors import BusinessError, UpstreamError
from office_agent_core.observability import record
from office_agent_core.settings import ProviderConfig, settings

#: 上游信封里「系统级失败」的下界（>= 该值一律按依赖故障处理）
_UPSTREAM_SYSTEM_CODE_FLOOR = 5000


class RemoteToolProvider:
    """一个上游工具提供方的客户端（provider_id 唯一，凭据只在内存）。"""

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        path: str = "/api/v1/agent-gateway/invoke",
        token: str = "",
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not provider_id.strip():
            raise ValueError("provider_id 不能为空")
        if not base_url.strip():
            raise ValueError(f"上游提供方 {provider_id} 缺少 base_url")
        self.provider_id = provider_id.strip()
        self._base_url = base_url.strip().rstrip("/")
        self._path = path if path.startswith("/") else f"/{path}"
        self._token = token
        self._timeout = float(timeout_seconds)
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_config(cls, provider_id: str, config: ProviderConfig) -> RemoteToolProvider:
        """按 Settings 里的提供方配置构建（token 用后即弃，不外泄到日志）。"""
        return cls(
            provider_id=provider_id,
            base_url=config.base_url,
            path=config.path,
            token=config.token.get_secret_value(),
            timeout_seconds=config.timeout_seconds,
        )

    @property
    def endpoint(self) -> str:
        """出站地址（溯源用；不含凭据，可安全落审计）。"""
        return f"{self._base_url}{self._path}"

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

    # ---------------- 出站调用 ----------------

    def _headers(self, *, trace_id: str, on_behalf_of: str) -> dict[str, str]:
        """组装出站头（trace/身份/调用方标识缺一不可）。"""
        headers = {
            "Content-Type": "application/json",
            "X-Trace-Id": trace_id,
            "X-Office-Agent": f"{settings.LINKAGE_CLIENT_NAME}/{settings.APP_VERSION}",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if on_behalf_of:
            headers["X-On-Behalf-Of"] = on_behalf_of
        return headers

    async def invoke(
        self,
        binding: RemoteBinding,
        args: dict[str, Any],
        *,
        trace_id: str,
        on_behalf_of: str = "",
    ) -> tuple[dict[str, Any], Provenance]:
        """调用上游工具，返回 ``(data, provenance)``。

        失败一律抛 BusinessError（业务拒绝）或 UpstreamError（依赖故障），
        由 executor 决定重试与熔断；本层不吞错、不返回空数据。
        """
        client = self._ensure_client()
        started = time.perf_counter()
        try:
            response = await client.post(
                self.endpoint,
                json={"tool": binding.tool, "args": args},
                headers=self._headers(trace_id=trace_id, on_behalf_of=on_behalf_of),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            self._trace(binding, started, ok=False, trace_id=trace_id)
            raise TimeoutError(
                f"上游提供方 {self.provider_id} 调用超时（>{self._timeout}s）"
            ) from exc
        except (httpx.HTTPError, OSError) as exc:
            self._trace(binding, started, ok=False, trace_id=trace_id)
            raise UpstreamError(
                f"上游提供方 {self.provider_id} 不可达（{type(exc).__name__}）"
            ) from exc

        # 溯源时刻取「收到响应」的实测时间，不是请求发起时间
        fetched_at = datetime.now(UTC).isoformat()
        payload = self._parse(response, binding)
        self._trace(binding, started, ok=True, trace_id=trace_id)
        return self._unwrap(payload, response, binding, fetched_at=fetched_at)

    def _parse(self, response: httpx.Response, binding: RemoteBinding) -> dict[str, Any]:
        """解析上游响应体为信封 dict；非 JSON 或非对象一律按依赖故障处理。"""
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise UpstreamError(
                f"上游提供方 {self.provider_id} 返回非 JSON（HTTP {response.status_code}）"
            ) from exc
        if not isinstance(payload, dict) or "code" not in payload:
            raise UpstreamError(
                f"上游提供方 {self.provider_id} 响应不符合信封契约（HTTP {response.status_code}）"
            )
        return payload

    def _unwrap(
        self,
        payload: dict[str, Any],
        response: httpx.Response,
        binding: RemoteBinding,
        *,
        fetched_at: str,
    ) -> tuple[dict[str, Any], Provenance]:
        """拆信封：code==0 取 data + 溯源；否则按号段分级抛错。"""
        code = int(payload.get("code", -1))
        message = str(payload.get("msg") or "")
        if code != 0:
            if code >= _UPSTREAM_SYSTEM_CODE_FLOOR or code < 0:
                # 上游自己系统级失败：算依赖故障，可重试、计熔断
                raise UpstreamError(
                    f"上游提供方 {self.provider_id} 返回系统级失败（code={code}）"
                    f"{'：' + message if message else ''}"
                )
            # 上游域内业务结果（如「对象不存在」）：原码透传，前端按码分支
            raise BusinessError(
                code,
                message or f"上游提供方 {self.provider_id} 拒绝了本次调用",
                response.status_code if 400 <= response.status_code < 500 else 400,
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            # 契约要求工具返回对象；形状不符宁可报错，也不塞一个空对象假装成功
            raise UpstreamError(
                f"上游提供方 {self.provider_id} 的 data 不是对象（{type(data).__name__}）"
            )
        provenance: Provenance = {
            "provider_id": self.provider_id,
            "source_endpoint": f"POST {self.endpoint}",
            "tool": binding.tool,
            "fetched_at": fetched_at,
            "upstream_trace_id": str(payload.get("trace_id") or ""),
        }
        return data, provenance

    def _trace(
        self, binding: RemoteBinding, started: float, *, ok: bool, trace_id: str
    ) -> None:
        """联动链路可观测（成功/失败都记，便于算上游可用率）。"""
        record(
            "agent.linkage",
            {
                "provider": self.provider_id,
                "tool": binding.tool,
                "ok": ok,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "trace_id": trace_id,
            },
        )