"""上游信封拆解（跨系统联动的唯一口径：HTTP 网关与 MCP 协议桥共用同一份分级语义）

链路：各提供方客户端（linkage.RemoteToolProvider / mcp-bridge.MCPProvider）收到上游结果
      → 本模块按号段分级 → 返回 (data, provenance) 或抛 BusinessError / UpstreamError。

为什么单独成文件：错误分级是跨系统的**唯一真相源**——
「上游域内业务码原码透传」与「上游 5xxx 是依赖故障（可重试、计熔断）」两条口径
若在两个协议实现里各写一份，迟早分叉成两套语义，制造第二真相源。
"""

from __future__ import annotations

from typing import Any

from office_agent_core.contracts import Provenance
from office_agent_core.errors import BusinessError, UpstreamError

#: 上游信封里「系统级失败」的下界（>= 该值一律按依赖故障处理）
UPSTREAM_SYSTEM_CODE_FLOOR = 5000


def unwrap_envelope(
    payload: dict[str, Any],
    *,
    provider_id: str,
    tool: str,
    source_endpoint: str,
    fetched_at: str,
    http_status: int = 200,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Provenance]:
    """拆上游信封：``code==0`` 取 data + 溯源；否则按号段分级抛错。

    失败分级（与 executor 的重试/熔断口径配套）：
    - 上游 1xxx/3xxx/4xxx（确定性业务结果）→ BusinessError **原码透传**，不重试、不计熔断；
    - 上游 5xxx / 非法 code → UpstreamError（可重试、计熔断）。

    ``extra`` 用于协议专属溯源字段（如 MCP 的 transport / protocol_version），
    禁止用它覆盖既有键——溯源字段的口径由本层统一钉死。
    """
    code = int(payload.get("code", -1))
    message = str(payload.get("msg") or "")
    if code != 0:
        if code >= UPSTREAM_SYSTEM_CODE_FLOOR or code < 0:
            raise UpstreamError(
                f"上游提供方 {provider_id} 返回系统级失败（code={code}）"
                f"{'：' + message if message else ''}"
            )
        raise BusinessError(
            code,
            message or f"上游提供方 {provider_id} 拒绝了本次调用",
            http_status if 400 <= http_status < 500 else 400,
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        # 契约要求工具返回对象；形状不符宁可报错，也不塞一个空对象假装成功
        raise UpstreamError(f"上游提供方 {provider_id} 的 data 不是对象（{type(data).__name__}）")
    provenance: Provenance = {
        "provider_id": provider_id,
        "source_endpoint": source_endpoint,
        "tool": tool,
        "fetched_at": fetched_at,
        "upstream_trace_id": str(payload.get("trace_id") or ""),
    }
    if extra:
        provenance.update(extra)
    return data, provenance


def looks_like_envelope(payload: Any) -> bool:
    """是否携带上游信封（含 ``code`` 键的对象）。

    MCP 工具结果既可能是裸对象（第三方 MCP Server 的常见形态），
    也可能是对端网关信封——凭本函数分流，两种都按同一套分级语义处理。
    """
    return isinstance(payload, dict) and "code" in payload
