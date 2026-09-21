"""内核执行链单测：Scope 硬拦 / 非幂等只调一次 / 熔断 / 溯源挂载 / 上游故障收口

口径说明：这些断言对应的是「跨系统联动的红线」，改动内核时它们必须继续成立。
"""

from __future__ import annotations

import httpx
import pytest

from office_agent_core import audit, executor, linkage, registry
from office_agent_core.contracts import RemoteBinding, ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.settings import settings

_URL = "http://up.test/api/v1/agent-gateway/invoke"
_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"order_id": {"type": "string", "title": "编号"}},
    "required": ["order_id"],
    "additionalProperties": False,
}


def _ctx(roles: list[str] | None = None) -> ToolContext:
    return ToolContext(tenant="t1", username="alice", roles=roles or ["*"], trace_id="tr-1")


def _provider(handler, provider_id: str = "up"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return linkage.RemoteToolProvider(
        provider_id=provider_id, base_url="http://up.test", token="tok", client=client
    )


@pytest.fixture(autouse=True)
def _isolate():
    """进程内单例（注册中心/提供方/熔断/审计）逐用例复位。"""
    registry.reset()
    linkage.reset()
    executor.reset_breakers()
    audit.reset()
    yield
    registry.reset()
    linkage.reset()
    executor.reset_breakers()
    audit.reset()


def _ok_handler(calls: list[int]):
    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            json={"code": 0, "msg": "操作成功", "data": {"order_id": "A1"}, "trace_id": "up-9"},
        )

    return handler


def _fail_handler(calls: list[int]):
    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("connection refused")

    return handler


async def test_scope_not_granted_raises_4006():
    """Scope 不命中直接 403/4006，绝不「没权限也执行」。"""
    registry.register(
        ToolSpec(
            name="x.read", scope="office:read", description="d", params={}, handler=_noop
        )
    )
    with pytest.raises(BusinessError) as excinfo:
        await executor.call(_ctx(roles=["office:write"]), name="x.read", args={})
    assert excinfo.value.code == ErrorCode.TOOL_SCOPE_DENIED


async def test_remote_success_carries_provenance():
    """远程工具成功时，结果同级必须挂上溯源（前端与审计都看得到数据出处）。"""
    calls: list[int] = []
    linkage.register_provider(_provider(_ok_handler(calls)))
    registry.register(
        ToolSpec(
            name="x.read",
            scope="s:read",
            description="d",
            params=_SCHEMA,
            remote=RemoteBinding(provider_id="up", tool="x.read"),
        )
    )
    data = await executor.call(_ctx(), name="x.read", args={"order_id": "A1"})
    assert data["status"] == "ok"
    assert data["provider_id"] == "up"
    assert data["result"] == {"order_id": "A1"}
    assert data["provenance"]["source_endpoint"] == f"POST {_URL}"
    assert data["provenance"]["fetched_at"]
    assert data["trace_id"] == "tr-1"
    assert calls == [1]


async def test_non_idempotent_remote_called_exactly_once():
    """非幂等工具恒只调一次——上游抖动也不许自动重发。"""
    calls: list[int] = []
    linkage.register_provider(_provider(_fail_handler(calls)))
    registry.register(
        ToolSpec(
            name="x.write",
            scope="s:read",
            description="d",
            params=_SCHEMA,
            remote=RemoteBinding(provider_id="up", tool="x.write"),
            idempotent=False,
        )
    )
    with pytest.raises(BusinessError) as excinfo:
        await executor.call(_ctx(), name="x.write", args={"order_id": "A1"})
    assert len(calls) == 1
    assert excinfo.value.code == ErrorCode.UPSTREAM_FAILED


async def test_idempotent_remote_retries_then_reports_upstream_failed(monkeypatch):
    """幂等工具在依赖故障下按设置重试，耗尽后以 5001 收口（不与本地 4008 糊成一个）。"""
    monkeypatch.setattr(settings, "AGENT_TOOL_MAX_RETRIES", 2)
    monkeypatch.setattr(settings, "AGENT_TOOL_RETRY_BACKOFF_SECONDS", 0.0)
    calls: list[int] = []
    linkage.register_provider(_provider(_fail_handler(calls)))
    registry.register(
        ToolSpec(
            name="x.read",
            scope="s:read",
            description="d",
            params=_SCHEMA,
            remote=RemoteBinding(provider_id="up", tool="x.read"),
        )
    )
    with pytest.raises(BusinessError) as excinfo:
        await executor.call(_ctx(), name="x.read", args={"order_id": "A1"})
    assert len(calls) == 3  # 1 次 + 2 次重试
    assert excinfo.value.code == ErrorCode.UPSTREAM_FAILED


async def test_upstream_business_error_is_not_retried(monkeypatch):
    """上游业务拒绝（4xxx）确定性结果：不重试、不计熔断、原码上抛。"""
    monkeypatch.setattr(settings, "AGENT_TOOL_MAX_RETRIES", 3)
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"code": 3001, "msg": "对象不存在", "data": None})

    linkage.register_provider(_provider(handler))
    registry.register(
        ToolSpec(
            name="x.read",
            scope="s:read",
            description="d",
            params=_SCHEMA,
            remote=RemoteBinding(provider_id="up", tool="x.read"),
        )
    )
    with pytest.raises(BusinessError) as excinfo:
        await executor.call(_ctx(), name="x.read", args={"order_id": "A1"})
    assert excinfo.value.code == 3001
    assert len(calls) == 1
    assert executor.breaker_snapshot("x.read")["failures"] == 0


async def test_breaker_opens_and_fails_fast(monkeypatch):
    """连续依赖故障达阈值即熔断，后续调用直接 4007 快失败（不再打上游）。"""
    monkeypatch.setattr(settings, "AGENT_TOOL_MAX_RETRIES", 0)
    monkeypatch.setattr(settings, "AGENT_TOOL_CIRCUIT_THRESHOLD", 2)
    calls: list[int] = []
    linkage.register_provider(_provider(_fail_handler(calls)))
    registry.register(
        ToolSpec(
            name="x.read",
            scope="s:read",
            description="d",
            params=_SCHEMA,
            remote=RemoteBinding(provider_id="up", tool="x.read"),
        )
    )
    for _ in range(2):
        with pytest.raises(BusinessError):
            await executor.call(_ctx(), name="x.read", args={"order_id": "A1"})
    with pytest.raises(BusinessError) as excinfo:
        await executor.call(_ctx(), name="x.read", args={"order_id": "A1"})
    assert excinfo.value.code == ErrorCode.TOOL_CIRCUIT_OPEN
    assert len(calls) == 2  # 第三次是快失败，没再出站


async def test_param_invalid_returns_1001():
    """入参不合 Schema：1001 + 中文可操作提示（注册中心契约即前端表单契约）。"""
    registry.register(
        ToolSpec(
            name="x.read",
            scope="s:read",
            description="d",
            params=_SCHEMA,
            remote=RemoteBinding(provider_id="up", tool="x.read"),
        )
    )
    with pytest.raises(BusinessError) as excinfo:
        await executor.call(_ctx(), name="x.read", args={"order_id": ""})
    assert excinfo.value.code == ErrorCode.PARAM_INVALID
    assert "编号" in excinfo.value.msg


async def test_audit_records_both_paths():
    """好/坏两条路径都必须留痕（工具调用透明可回放）。"""
    registry.register(
        ToolSpec(name="x.read", scope="s:read", description="d", params={}, handler=_noop)
    )
    await executor.call(_ctx(), name="x.read", args={})
    assert audit.count() == 1
    assert audit.recent(1)[0]["ok"] is True

    registry.register(
        ToolSpec(
            name="x.fail",
            scope="s:read",
            description="d",
            params=_SCHEMA,
            remote=RemoteBinding(provider_id="up", tool="x.fail"),
        )
    )
    with pytest.raises(BusinessError):
        await executor.call(_ctx(), name="x.fail", args={"order_id": ""})
    assert audit.count() == 2
    assert audit.recent(1)[0]["ok"] is False


async def _noop(_ctx: ToolContext, _args: dict[str, object]) -> dict[str, object]:
    return {"ok": True}