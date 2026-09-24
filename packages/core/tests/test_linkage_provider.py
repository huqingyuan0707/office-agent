"""联动层单测：出站头三件套 / 溯源实测 / 错误分级

全部走 ``httpx.MockTransport``，不打真实网络；断言的目标都是「跨系统联动的契约」，
而不是实现细节：头必须齐、溯源必须实测、上游码必须不被内核翻译。
"""

from __future__ import annotations

import json

import httpx
import pytest

from office_agent_core import linkage
from office_agent_core.contracts import RemoteBinding
from office_agent_core.errors import BusinessError, UpstreamError
from office_agent_core.settings import ProviderConfig

_ENDPOINT = "http://up.test/api/v1/agent-gateway/invoke"


def _provider(handler, *, token: str = "tok") -> linkage.RemoteToolProvider:
    """用 MockTransport 造一个「假上游」客户端。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return linkage.RemoteToolProvider(
        provider_id="up", base_url="http://up.test", token=token, client=client
    )


@pytest.fixture(autouse=True)
def _clean_providers():
    """每个用例前后都清空提供方（进程内单例，防用例间串味）。"""
    linkage.reset()
    yield
    linkage.reset()


async def test_outbound_headers_and_provenance():
    """出站头必须齐（trace/身份/调用方/授权），溯源必须由本层实测填写。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["trace"] = request.headers.get("x-trace-id")
        seen["on_behalf_of"] = request.headers.get("x-on-behalf-of")
        seen["authorization"] = request.headers.get("authorization")
        seen["client"] = request.headers.get("x-office-agent")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"code": 0, "msg": "操作成功", "data": {"order_id": "A1"}, "trace_id": "up-1"},
        )

    provider = _provider(handler)
    linkage.register_provider(provider)

    data, provenance = await provider.invoke(
        RemoteBinding(provider_id="up", tool="x.read"),
        {"order_id": "A1"},
        trace_id="tr-1",
        on_behalf_of="alice",
    )

    assert data == {"order_id": "A1"}
    assert seen["body"] == {"tool": "x.read", "args": {"order_id": "A1"}}
    assert seen["trace"] == "tr-1"
    assert seen["on_behalf_of"] == "alice"
    assert seen["authorization"] == "Bearer tok"
    assert str(seen["client"]).startswith("office-agent/")
    # 溯源由联动层实测：地址是本层端点、时刻是本层收响应时刻、上游 trace 来自信封
    assert provenance["provider_id"] == "up"
    assert provenance["source_endpoint"] == f"POST {_ENDPOINT}"
    assert provenance["tool"] == "x.read"
    assert provenance["fetched_at"]
    assert provenance["upstream_trace_id"] == "up-1"


async def test_upstream_business_code_passthrough():
    """上游域内业务码（4xxx 段）必须原码上抛，内核不做任何翻译。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 4006, "msg": "权限不足", "data": None})

    provider = _provider(handler)
    with pytest.raises(BusinessError) as excinfo:
        await provider.invoke(RemoteBinding("up", "x.read"), {}, trace_id="tr")
    assert excinfo.value.code == 4006
    assert excinfo.value.msg == "权限不足"


async def test_upstream_system_failure_is_dependency_error():
    """上游 5xxx 是依赖故障（可重试、计熔断），绝不能当业务结果抛给前端。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": 5000, "msg": "内部错误", "data": None})

    provider = _provider(handler)
    with pytest.raises(UpstreamError):
        await provider.invoke(RemoteBinding("up", "x.read"), {}, trace_id="tr")


async def test_unreachable_upstream_is_dependency_error():
    """连不通属依赖故障，不是「工具调用失败」的本地错误。"""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = _provider(handler)
    with pytest.raises(UpstreamError):
        await provider.invoke(RemoteBinding("up", "x.read"), {}, trace_id="tr")


async def test_malformed_envelope_is_dependency_error():
    """响应不合信封契约（非 JSON / 缺 code / data 非对象）一律按依赖故障处理。"""

    def missing_code(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"msg": "ok"})

    with pytest.raises(UpstreamError):
        await _provider(missing_code).invoke(RemoteBinding("up", "x.read"), {}, trace_id="tr")

    def bad_data(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": ["not", "an", "object"]})

    with pytest.raises(UpstreamError):
        await _provider(bad_data).invoke(RemoteBinding("up", "x.read"), {}, trace_id="tr")


async def test_unconfigured_provider_raises_actionable_error():
    """提供方没配就报错并给出可照做的配置方式——绝不静默降级成假数据。"""
    with pytest.raises(BusinessError) as excinfo:
        linkage.provider_of("nobody")
    assert excinfo.value.code == 5001
    assert excinfo.value.http_status == 503
    assert "LINKAGE_PROVIDERS" in excinfo.value.msg


def test_configure_from_settings_claims_only_own_transport():
    """协议分流：内核只认领 transport=http 的条目，其余留给对应协议桥包（如 mcp）。"""
    providers = {
        "gw": ProviderConfig(base_url="http://gw.test"),
        "mcp-up": ProviderConfig(base_url="http://mcp.test", path="/mcp", transport="mcp"),
    }

    ids = linkage.configure_from_settings(providers)

    assert ids == ["gw"]
    assert isinstance(linkage.get_provider("gw"), linkage.RemoteToolProvider)
    assert linkage.get_provider("mcp-up") is None
    assert linkage.configured("mcp-up") is False
