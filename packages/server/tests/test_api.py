"""壳层单测：信封 / trace 贯穿 / 鉴权 / 工具调用与溯源 / 审计落库 / 治理状态"""

from __future__ import annotations

import httpx

from office_agent_core import linkage

_ENDPOINT = "POST http://up.test/api/v1/agent-gateway/invoke"


def _login(client, username: str = "admin", password: str = "admin123") -> str:
    """登录取 token（种子账号由 lifespan 建好）。"""
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    return str(body["data"]["token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _install_upstream(payload: dict, *, status: int = 200, calls: list | None = None) -> None:
    """把 ecommerce 提供方换成假上游（MockTransport），记录出站次数。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, json=payload)

    linkage.register_provider(
        linkage.RemoteToolProvider(
            provider_id="ecommerce",
            base_url="http://up.test",
            token="tp",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    )


def test_inbound_trace_id_is_reused(client):
    """入站 X-Trace-Id 必须沿用：跨系统同一条 trace 是排障红线。"""
    resp = client.get("/health", headers={"X-Trace-Id": "tr-inbound"})
    assert resp.status_code == 200
    assert resp.headers["X-Trace-Id"] == "tr-inbound"
    assert "X-Elapsed-Ms" in resp.headers

    body = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin123"},
        headers={"X-Trace-Id": "tr-login"},
    ).json()
    assert body["trace_id"] == "tr-login"


def test_trace_id_generated_when_absent(client):
    """客户端没带 trace 时由中间件新生成，并原样回写响应体与响应头。"""
    resp = client.get("/health")
    trace_id = resp.headers["X-Trace-Id"]
    assert trace_id
    assert client.get("/health").headers["X-Trace-Id"] != trace_id


def test_login_failure_envelope(client):
    """验密失败：HTTP 401 + code 1002 中文提示（前端据此分支）。"""
    resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == 1002
    assert body["data"] is None
    assert body["trace_id"]


def test_missing_token_is_1002(client):
    """未登录：路由级依赖 401 也统一收口成信封，绝不裸抛。"""
    resp = client.get("/api/v1/agent/tools")
    assert resp.status_code == 401
    assert resp.json()["code"] == 1002


def test_plugin_tools_are_registered(client):
    """插件在提供方已配置时注册只读工具，并在清单里透出 provider_id（不含地址与凭据）。"""
    token = _login(client)
    body = client.get("/api/v1/agent/tools", headers=_auth(token)).json()
    assert body["code"] == 0
    names = {item["name"] for item in body["data"]["items"]}
    assert {"order.query", "logistics.query", "stock.query", "coupon.query", "kb.retrieve"} <= names
    order = next(item for item in body["data"]["items"] if item["name"] == "order.query")
    assert order["scope"] == "order:read"
    assert order["provider_id"] == "ecommerce"
    assert order["requires_approval"] is False
    assert "up.test" not in str(order)  # 上游地址绝不进工具出参


def test_invoke_remote_tool_returns_provenance(client):
    """远程工具调用：结果带溯源、trace 与入站一致、出站只发一次。"""
    token = _login(client)
    calls: list = []
    _install_upstream(
        {"code": 0, "msg": "操作成功", "data": {"order_id": "A1"}, "trace_id": "up-77"},
        calls=calls,
    )
    resp = client.post(
        "/api/v1/agent/tools/order.query/invoke",
        json={"args": {"order_id": "A1"}},
        headers={**_auth(token), "X-Trace-Id": "tr-invoke"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["status"] == "ok"
    assert data["result"] == {"order_id": "A1"}
    assert data["trace_id"] == "tr-invoke"
    assert data["provider_id"] == "ecommerce"
    assert data["provenance"]["source_endpoint"] == _ENDPOINT
    assert data["provenance"]["fetched_at"]
    assert data["provenance"]["upstream_trace_id"] == "up-77"
    assert len(calls) == 1
    assert calls[0].headers["x-trace-id"] == "tr-invoke"
    assert calls[0].headers["x-on-behalf-of"] == "admin"


def test_invoke_upstream_business_code_passthrough(client):
    """上游业务拒绝（4xxx）原码透传到前端，不被内核翻译。"""
    token = _login(client)
    _install_upstream({"code": 3001, "msg": "对象不存在", "data": None})
    resp = client.post(
        "/api/v1/agent/tools/order.query/invoke",
        json={"args": {"order_id": "A1"}},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 3001
    assert body["msg"] == "对象不存在"


def test_invoke_upstream_failure_becomes_5001(client):
    """上游系统级失败收口成 5001（不是 5000 也不是 4008），前端可按码提示「稍后重试」。"""
    token = _login(client)
    _install_upstream({"code": 5000, "msg": "内部错误", "data": None}, status=500)
    resp = client.post(
        "/api/v1/agent/tools/order.query/invoke",
        json={"args": {"order_id": "A1"}},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 5001


def test_invoke_param_invalid_is_1001(client):
    """参数不合 Schema：1001 + 中文提示。"""
    token = _login(client)
    resp = client.post(
        "/api/v1/agent/tools/order.query/invoke",
        json={"args": {}},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 1001
    # 只断言通用文案形状：参数 label 由插件自己的 Schema 提供，内核不预设业务词
    assert "缺少必填参数" in body["msg"]


def test_unknown_tool_is_4005(client):
    token = _login(client)
    resp = client.post("/api/v1/agent/tools/nope/invoke", json={"args": {}}, headers=_auth(token))
    assert resp.status_code == 404
    assert resp.json()["code"] == 4005


def test_tasks_and_approvals_are_empty_not_error(client):
    """真实空数据返回 []，不报错（前端空态自行渲染）。"""
    token = _login(client)
    assert client.get("/api/v1/tasks", headers=_auth(token)).json()["data"] == []
    assert client.get("/api/v1/approvals", headers=_auth(token)).json()["data"] == []


def test_approval_reject_requires_reason(client):
    """不存在的审批单先 404；驳回理由必填的口径由 _decide 保证（此处验证 404 分支）。"""
    token = _login(client)
    resp = client.post("/api/v1/approvals/nope/reject", json={"reason": ""}, headers=_auth(token))
    assert resp.status_code == 404
    assert resp.json()["code"] == 1004


def test_direct_approval_executes_with_applicant_roles(client):
    """回归：批准后必须以**申请人真实角色**执行。

    旧口径 decide_approval 传 roles=[]，被 executor 内 ensure_allowed 硬拦 4006，
    直批执行路径必失败（信封仍 200，故障只藏在 execution_result 里）——
    模式②回流的地基就是这条路径，锁死它。
    """
    admin = _auth(_login(client))
    reviewer = _auth(_login(client, "reviewer", "reviewer123"))
    resp = client.post(
        "/api/v1/agent/tools/office.todo.create/invoke",
        json={"args": {"title": "直批回归", "priority": "low"}},
        headers=admin,
    ).json()
    assert resp["code"] == 0
    assert resp["data"]["status"] == "pending_approval"

    decided = client.post(
        f"/api/v1/approvals/{resp['data']['approval_id']}/approve",
        json={"reason": "回归批准"},
        headers=reviewer,
    ).json()
    assert decided["code"] == 0
    assert decided["data"]["status"] == "approved"
    execution = decided["data"]["execution_result"]
    assert execution["status"] == "ok", f"直批执行路径被拦：{execution}"
    assert execution["result"]["owner"] == "admin"  # 以提交人身份生效


def test_governance_status_surfaces_linkage_health(client):
    """治理状态：工具数、已配/未配提供方、待办审批、指标一屏可见。"""
    token = _login(client)
    body = client.get("/api/v1/governance/status", headers=_auth(token)).json()
    assert body["code"] == 0
    data = body["data"]
    assert data["tools"]["remote"] >= 5
    assert [item["provider_id"] for item in data["providers"]] == ["ecommerce"]
    assert data["providers_missing"] == []
    assert "test-token" not in str(data)  # 凭据绝不进任何出参


async def test_tool_call_audit_is_persisted_with_trace(client):
    """审计必须落库且带 trace：跨系统「同一 trace 两侧可查」靠的就是这一行。

    这里用独立引擎读同一个 SQLite 文件（避免与测试客户端所在事件循环共享连接池）。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from office_agent_core.settings import settings

    token = _login(client)
    _install_upstream({"code": 0, "msg": "操作成功", "data": {"order_id": "A1"}, "trace_id": "u1"})
    resp = client.post(
        "/api/v1/agent/tools/order.query/invoke",
        json={"args": {"order_id": "A1"}},
        headers={**_auth(token), "X-Trace-Id": "tr-audit"},
    )
    assert resp.status_code == 200

    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "select name, tenant, username, result from tool_calls where trace_id = :t"
                    ),
                    {"t": "tr-audit"},
                )
            ).all()
    finally:
        await engine.dispose()

    assert len(rows) == 1
    name, tenant, username, result = rows[0]
    assert name == "order.query"
    assert tenant == "demo-tenant"
    assert username == "admin"
    assert "agent-gateway/invoke" in result  # 溯源随审计一起落库


async def test_failed_call_audit_is_persisted_too(client):
    """失败路径同样落审计：只读工具调用失败（上游故障）也要能在审计里查到。

    审计是 append-only 留痕，不能因业务失败被会话回滚抹掉——
    否则「同一 trace 两侧可查」在故障场景就断了。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from office_agent_core.settings import settings

    token = _login(client)
    _install_upstream({"code": 5000, "msg": "内部错误", "data": None}, status=500)
    resp = client.post(
        "/api/v1/agent/tools/order.query/invoke",
        json={"args": {"order_id": "A1"}},
        headers={**_auth(token), "X-Trace-Id": "tr-audit-fail"},
    )
    assert resp.json()["code"] == 5001

    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("select name, result from tool_calls where trace_id = :t"),
                    {"t": "tr-audit-fail"},
                )
            ).all()
    finally:
        await engine.dispose()

    assert len(rows) == 1
    name, result = rows[0]
    assert name == "order.query"
    assert "failed" in result
