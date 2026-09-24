"""IM 审批通知单测：四厂商消息体 / 加签 / 降级不阻断 / 审批流挂钩

全程 MockTransport，不打真实网络；断言的是「通知绝不阻断审批流」这条红线，
以及消息体形状与签名算法（对端按此校验，错了就是静默丢消息）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from base64 import b64encode

import httpx
import pytest
from pydantic import SecretStr

from office_agent_core.settings import settings
from office_agent_server.services import im_notifier

WEBHOOK = "https://im.test/hook"


@pytest.fixture(autouse=True)
def _reset_im_settings():
    """每个用例前后复位 IM 配置与注入 transport（Settings 是进程内单例）。"""
    original = (
        settings.IM_WEBHOOK_URL,
        settings.IM_WEBHOOK_TYPE,
        settings.IM_WEBHOOK_SECRET,
        settings.IM_WEBHOOK_MAX_ITEMS,
    )
    im_notifier.set_transport(None)
    yield
    (
        settings.IM_WEBHOOK_URL,
        settings.IM_WEBHOOK_TYPE,
        settings.IM_WEBHOOK_SECRET,
        settings.IM_WEBHOOK_MAX_ITEMS,
    ) = original
    im_notifier.set_transport(None)


class _Recorder:
    """记录出站请求的假对端（可指定状态码与回执体）。"""

    def __init__(self, *, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self.body = body if body is not None else {"code": 0}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, json=self.body)

    def transport(self) -> httpx.MockTransport:
        """MockTransport 形态的传输层（注入后永不出网）。"""
        return httpx.MockTransport(self)

    def payload(self) -> dict:
        """最后一次出站的 JSON 体。"""
        return json.loads(self.requests[-1].content)


def _configure(vendor: str = "generic", *, secret: str = "", url: str = WEBHOOK) -> None:
    """按用例改 Settings（进程内单例，属性直接赋值即可生效；密钥字段须用 SecretStr）。"""
    settings.IM_WEBHOOK_URL = url
    settings.IM_WEBHOOK_TYPE = vendor
    settings.IM_WEBHOOK_SECRET = SecretStr(secret)


async def test_not_configured_degrades_without_network():
    """未配置 URL：直接降级返回，不发任何网络请求。"""
    settings.IM_WEBHOOK_URL = ""
    recorder = _Recorder()
    im_notifier.set_transport(recorder.transport())

    result = await im_notifier.fire(kind="approval_created", title="标题", content="内容")

    assert result["sent"] is False
    assert result["reason"] == "not_configured"
    assert recorder.requests == []


async def test_generic_payload_carries_context():
    """generic 消息体带全量上下文（单号/租户/申请人），供第三方系统直接消费。"""
    _configure()
    recorder = _Recorder()
    im_notifier.set_transport(recorder.transport())

    result = await im_notifier.fire(
        kind="approval_created",
        title="【待审批】",
        content="工具：office.todo.create",
        ref_id="ap-1",
        tenant="demo-tenant",
        username="alice",
    )

    assert result["sent"] is True
    body = recorder.payload()
    assert body["source"] == "office-agent"
    assert body["kind"] == "approval_created"
    assert body["ref_id"] == "ap-1"
    assert body["tenant"] == "demo-tenant"
    assert body["username"] == "alice"


@pytest.mark.parametrize(
    ("vendor", "expected"),
    [
        ("feishu", {"msg_type": "text"}),
        ("dingtalk", {"msgtype": "text"}),
        ("wecom", {"msgtype": "text"}),
    ],
)
async def test_vendor_payload_shapes(vendor: str, expected: dict):
    """三厂商消息体形状必须逐字对：形状错了对端直接拒收（静默丢消息）。"""
    _configure(vendor)
    recorder = _Recorder()
    im_notifier.set_transport(recorder.transport())

    await im_notifier.fire(kind="approval_created", title="标题", content="内容")

    body = recorder.payload()
    for key, value in expected.items():
        assert body[key] == value
    text = body.get("text", {}).get("content") or body.get("content", {}).get("text")
    assert "标题" in text and "内容" in text


async def test_unknown_vendor_falls_back_to_generic():
    """厂商取值不受支持时按 generic 发送（告警但不静默丢消息）。"""
    _configure("unknown-bot")

    assert im_notifier.vendor() == "generic"
    assert im_notifier.build_message(title="t", content="c", kind="approval_created")["source"] == (
        "office-agent"
    )


async def test_dingtalk_signature_is_appended_to_url():
    """钉钉签名按官方算法（key=secret，msg=timestamp\\nsecret）拼到 URL query。"""
    _configure("dingtalk", secret="sec-1")
    recorder = _Recorder()
    im_notifier.set_transport(recorder.transport())

    await im_notifier.fire(kind="approval_created", title="标题", content="内容")

    url = recorder.requests[-1].url
    stamp = url.params.get("timestamp")
    sign = url.params.get("sign")
    assert stamp and sign
    expected = b64encode(
        hmac.new(b"sec-1", f"{stamp}\nsec-1".encode(), hashlib.sha256).digest()
    ).decode()
    assert sign == expected


async def test_feishu_signature_is_in_body():
    """飞书签名放消息体顶层 timestamp + sign。"""
    _configure("feishu", secret="sec-1")
    recorder = _Recorder()
    im_notifier.set_transport(recorder.transport())

    await im_notifier.fire(kind="approval_created", title="标题", content="内容")

    body = recorder.payload()
    assert "timestamp" in body and "sign" in body
    expected = b64encode(
        hmac.new(f"{body['timestamp']}\nsec-1".encode(), b"", hashlib.sha256).digest()
    ).decode()
    assert body["sign"] == expected


async def test_vendor_error_code_is_degraded():
    """HTTP 200 但厂商回执 errcode 非 0 → 判定投递失败（不假装成功）。"""
    _configure("wecom")
    recorder = _Recorder(body={"errcode": 93000, "errmsg": "invalid webhook url"})
    im_notifier.set_transport(recorder.transport())

    result = await im_notifier.fire(kind="approval_created", title="标题", content="内容")

    assert result["sent"] is False
    assert "93000" in result["reason"]


async def test_http_error_is_degraded():
    """HTTP 4xx/5xx → 降级返回，不抛错。"""
    _configure()
    recorder = _Recorder(status_code=500)
    im_notifier.set_transport(recorder.transport())

    result = await im_notifier.fire(kind="approval_created", title="标题", content="内容")

    assert result["sent"] is False
    assert result["status_code"] == 500


async def test_network_failure_never_raises():
    """连不通只降级：通知失败绝不能把审批流带崩（红线）。"""
    _configure()

    def broken(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    im_notifier.set_transport(httpx.MockTransport(broken))

    result = await im_notifier.fire(kind="approval_created", title="标题", content="内容")

    assert result["sent"] is False
    assert "ConnectError" in result["reason"]


async def test_stale_notification_aggregates_items():
    """超时催办聚合一条消息：列出前 N 条并报出未列出的余量（绝不逐单刷屏）。"""
    _configure()
    settings.IM_WEBHOOK_MAX_ITEMS = 1
    recorder = _Recorder()
    im_notifier.set_transport(recorder.transport())
    items = [
        {
            "approval_id": "ap-1",
            "action": "office.todo.create",
            "target": "待办",
            "applicant": "alice",
            "hours": 30,
        },
        {
            "approval_id": "ap-2",
            "action": "ticket.create",
            "target": "工单",
            "applicant": "bob",
            "hours": 28,
        },
    ]

    result = await im_notifier.notify_approval_stale(
        tenant="demo-tenant", items=items, stale_hours=24
    )

    assert result["sent"] is True
    body = recorder.payload()
    assert body["kind"] == "approval_stale"
    assert "2 单" in body["title"]
    assert "另有 1 单未列出" in body["content"]
    assert "ap-2" not in body["content"]  # 只列前 N 条
