"""IM 审批通知出站（群机器人 webhook；审批单产生/超时时提醒复核人）

职责：
- fire()：通用出站——按 Settings.IM_WEBHOOK_TYPE 组装消息体（generic/feishu/dingtalk/wecom），
  带签名（飞书/钉钉）出站，**失败降级绝不抛错**；
- notify_approval_created()：审批单落单时提醒群（有新单待处理）；
- notify_approval_urge()：申请人一键催办时提醒群（单条，PRD §2.3）；
- notify_approval_stale()：审批超时扫描命中时提醒群（聚合一条，绝不逐单刷屏）。

链路：api/tools.invoke → services.approval_flow.create_approval → 本模块（旁路，不参与审批状态流转）；
      POST /notifications/scan → services.notifications.scan_notifications → 本模块。

红线（对齐 `docs/ADR-0004-MCP协议桥与IM审批通知出站.md` §3 与 AGENTS.md §3）：
- 旁路而非主链：IM 是通知，不是工具调用——不进 linkage 出站层，不计熔断，不改审批状态；
- 降级绝不阻断：未配置 URL 直接返回 ``{"sent": False, "reason": "not_configured"}`` 不发网络；
  出站任何异常（超时/连不通/非 JSON/厂商错误码）只记日志与可观测，**绝不向调用方抛错**；
- 凭据只走环境变量（Settings.IM_WEBHOOK_SECRET），绝不入库、绝不落日志；
- 消息只含「有单要处理」类信息（单号/工具/申请人），不带业务明细，防群泄露。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from office_agent_core.observability import record
from office_agent_core.settings import settings

logger = logging.getLogger(__name__)

KIND_APPROVAL_CREATED = "approval_created"
KIND_APPROVAL_STALE = "approval_stale"
KIND_APPROVAL_URGE = "approval_urge"

#: 支持的厂商消息体（其余取值按 generic 处理并告警，不静默丢消息）
VENDOR_GENERIC = "generic"
VENDOR_FEISHU = "feishu"
VENDOR_DINGTALK = "dingtalk"
VENDOR_WECOM = "wecom"
VENDORS = (VENDOR_GENERIC, VENDOR_FEISHU, VENDOR_DINGTALK, VENDOR_WECOM)

#: 厂商回执里表示「业务成功」的字段（存在且非 0 即视为投递失败）
_VENDOR_CODE_FIELDS = ("errcode", "code")

#: 测试注入点（生产为 None → 走真实网络）
_transport: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """注入出站 transport（仅测试用；None = 恢复真实网络）。"""
    global _transport
    _transport = transport


def vendor() -> str:
    """当前厂商（未知取值按 generic 处理并告警一次，绝不静默丢消息）。"""
    value = (settings.IM_WEBHOOK_TYPE or VENDOR_GENERIC).strip().lower()
    if value not in VENDORS:
        logger.warning("IM_WEBHOOK_TYPE=%s 不是受支持的厂商，已按 generic 消息体发送", value)
        return VENDOR_GENERIC
    return value


def build_message(
    *,
    title: str,
    content: str,
    kind: str,
    ref_id: str = "",
    tenant: str = "",
    username: str = "",
) -> dict[str, Any]:
    """按厂商组装消息体（凭据不参与消息体，签名另在 URL/顶层字段上算）。"""
    text = f"{title}\n{content}".strip()
    current = vendor()
    if current == VENDOR_FEISHU:
        return {"msg_type": "text", "content": {"text": text}}
    if current in (VENDOR_DINGTALK, VENDOR_WECOM):
        return {"msgtype": "text", "text": {"content": text}}
    # generic：本仓库自定义结构，第三方系统可直接消费全部上下文
    return {
        "source": "office-agent",
        "kind": kind,
        "title": title,
        "content": content,
        "ref_id": ref_id,
        "tenant": tenant,
        "username": username,
    }


def _timestamp() -> str:
    """签名时间戳（毫秒；飞书要求与服务器时间差在 1 小时内）。"""
    return str(int(time.time() * 1000))


def _hmac_base64(secret: str, message: str) -> str:
    """HMAC-SHA256 → Base64（飞书/钉钉签名共用算法）。"""
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def apply_signature(
    url: str, payload: dict[str, Any], *, secret: str
) -> tuple[str, dict[str, Any]]:
    """按厂商规则加签：钉钉拼到 URL query，飞书放顶层 timestamp/sign，其余不加签。"""
    if not secret:
        return url, payload
    current = vendor()
    stamp = _timestamp()
    if current == VENDOR_DINGTALK:
        sign = _hmac_base64(secret, f"{stamp}\n{secret}")
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{urlencode({'timestamp': stamp, 'sign': sign})}", payload
    if current == VENDOR_FEISHU:
        signed = dict(payload)
        signed["timestamp"] = stamp
        signed["sign"] = _hmac_base64(f"{stamp}\n{secret}", "")
        return url, signed
    return url, payload


def _vendor_failure(response: httpx.Response) -> str:
    """厂商回执里的业务错误（HTTP 200 但 errcode/code 非 0 也算投递失败）。"""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return ""  # 非 JSON 回执不看业务码（如自建 webhook 回空体）
    if not isinstance(payload, dict):
        return ""
    for field in _VENDOR_CODE_FIELDS:
        code = payload.get(field)
        if isinstance(code, int) and code != 0:
            return f"{field}={code} {payload.get('errmsg') or payload.get('msg') or ''}".strip()
    return ""


async def fire(
    *,
    kind: str,
    title: str,
    content: str,
    ref_id: str = "",
    tenant: str = "",
    username: str = "",
) -> dict[str, Any]:
    """通用出站（唯一的网络出口）：**任何失败都降级为 sent=False，绝不抛错**。

    返回 ``{"sent", "vendor", "reason", "status_code", "latency_ms"}``，供调用方留痕与巡检。
    """
    url = (settings.IM_WEBHOOK_URL or "").strip()
    current = vendor()
    if not url:
        return {"sent": False, "vendor": current, "reason": "not_configured", "status_code": 0}
    payload = build_message(
        title=title, content=content, kind=kind, ref_id=ref_id, tenant=tenant, username=username
    )
    target, payload = apply_signature(
        url, payload, secret=settings.IM_WEBHOOK_SECRET.get_secret_value()
    )
    timeout = float(settings.IM_WEBHOOK_TIMEOUT_SECONDS)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=_transport) as client:
            response = await client.post(
                target,
                json=payload,
                headers={"X-Office-Agent": f"{settings.APP_NAME}/{settings.APP_VERSION}"},
            )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "IM 通知发送失败（已降级，不影响业务）：kind=%s err=%s", kind, str(exc)[:200]
        )
        record(
            "agent.im_webhook",
            {"kind": kind, "vendor": current, "ok": False, "latency_ms": latency_ms},
        )
        return {
            "sent": False,
            "vendor": current,
            "reason": f"{type(exc).__name__}: {str(exc)[:120]}",
            "status_code": 0,
            "latency_ms": latency_ms,
        }

    latency_ms = int((time.perf_counter() - started) * 1000)
    failure = ""
    if response.status_code >= 400:
        failure = f"HTTP {response.status_code}"
    else:
        failure = _vendor_failure(response)
    ok = not failure
    if not ok:
        logger.warning("IM 通知被对端拒绝（已降级）：kind=%s reason=%s", kind, failure)
    record(
        "agent.im_webhook", {"kind": kind, "vendor": current, "ok": ok, "latency_ms": latency_ms}
    )
    return {
        "sent": ok,
        "vendor": current,
        "reason": failure,
        "status_code": response.status_code,
        "latency_ms": latency_ms,
    }


async def notify_approval_created(
    *, tenant: str, applicant: str, tool_name: str, target: str, approval_id: str
) -> dict[str, Any]:
    """审批单落单 → 提醒群里有新单待处理（只给定位信息，不带业务明细）。"""
    return await fire(
        kind=KIND_APPROVAL_CREATED,
        title="【待审批】有新的审批单待处理",
        content=(
            f"工具：{tool_name}\n申请单号：{approval_id}\n申请人：{applicant}\n事项：{target}\n"
            "请到审批中心复核（写动作经批准后才生效）"
        ),
        ref_id=approval_id,
        tenant=tenant,
        username=applicant,
    )


async def notify_approval_urge(
    *, tenant: str, applicant: str, tool_name: str, target: str, approval_id: str
) -> dict[str, Any]:
    """一键催办（PRD §2.3）：申请人主动提醒复核人处理某一 pending 单（单条，非聚合）。

    旁路口径与落单/超时提醒一致：不改审批状态、不进 linkage、失败只降级不抛错。
    """
    return await fire(
        kind=KIND_APPROVAL_URGE,
        title="【催办】申请人提醒处理审批单",
        content=(
            f"工具：{tool_name}\n申请单号：{approval_id}\n申请人：{applicant}\n事项：{target}\n"
            "申请人已催办，请尽快到审批中心复核"
        ),
        ref_id=approval_id,
        tenant=tenant,
        username=applicant,
    )


async def notify_approval_stale(
    *, tenant: str, items: list[dict[str, Any]], stale_hours: float
) -> dict[str, Any]:
    """审批超时 → 聚合一条提醒（列前 N 条，防逐单刷屏；N 由 Settings 控制）。"""
    limit = max(0, int(settings.IM_WEBHOOK_MAX_ITEMS))
    shown = items[:limit] if limit else []
    lines = [
        f"{index}. {item.get('action')}（{item.get('target')}）申请人 {item.get('applicant')}"
        f" 已 {item.get('hours')} 小时未处理"
        for index, item in enumerate(shown, 1)
    ]
    tail = f"\n另有 {len(items) - len(shown)} 单未列出" if len(items) > len(shown) else ""
    return await fire(
        kind=KIND_APPROVAL_STALE,
        title=f"【超时催办】{len(items)} 单审批已超过 {int(stale_hours)} 小时未处理",
        content="\n".join(lines) + tail + "\n请尽快复核或转交他人处理",
        ref_id=items[0].get("approval_id", "") if items else "",
        tenant=tenant,
    )
