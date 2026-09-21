"""外部客服系统（电商）桥接插件 —— 全仓库唯一允许出现电商语义的文件；可选插件，缺省不影响独立运行。

职责：当且仅当 ECOMMERCE_ENABLED=true 且 ECOMMERCE_TOKEN 非空时，把电商客服系统的只读查询能力
以 ToolSpec 形式注册进注册中心：
① ecommerce.screen.summary：GET {ECOMMERCE_API_BASE}/api/v1/screen/summary；
② ecommerce.finance.bills：GET {ECOMMERCE_API_BASE}/api/v1/finance/bills?page&size（默认 1/20）。
两个工具均只读（scope=office:read，needs_approval=False）。

红线（对齐设计文档）：
- 数据不出域：只走 HTTP + Bearer token 调电商 REST API，绝不直连电商数据库（§7.1 星型拓扑）；
- 溯源：响应必带 source="ecommerce-api" + fetched_at（§7.2 模式① pull 必带溯源）；
- 可选性：注册期先探测连通性，电商不在线 → 跳过注册并 log warning，独立运行零影响（§7.5 可选插件红线）。
链路：main.lifespan → register_if_enabled() → 探测 /healthz → registry.register；
executor 经 handler → httpx → 电商 REST API → 解 {ok,data} 信封取 data 回流。
对齐：docs/office-agent仓库骨架与内核提取方案.md §3（connectors 不迁主包→改造为插件）、§7.2/§7.5。
"""

import logging
from datetime import datetime, timezone

import httpx

from office_agent import registry
from office_agent.config import settings
from office_agent.contracts import SCOPE_READ, ToolError, ToolSpec

logger = logging.getLogger("office_agent.tools_ecommerce")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _probe() -> bool:
    """注册期连通性探测：服务端有任何 HTTP 响应即视为在线（不看状态码），连接异常视为离线。"""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3.0) as client:
            await client.get(f"{settings.ECOMMERCE_API_BASE}/healthz")
        return True
    except Exception as exc:  # noqa: BLE001 —— 探测阶段任何失败都等价于「不可达」
        logger.warning("外部系统探测失败（%s）：%s", settings.ECOMMERCE_API_BASE, exc)
        return False


async def _get_data(path: str, params: dict | None = None):
    """统一 HTTP 桥接：Bearer token → 调电商 REST API → 校验 {ok,data} 信封 → 返回 data。"""
    headers = {"Authorization": f"Bearer {settings.ECOMMERCE_TOKEN}"}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=float(settings.TOOL_TIMEOUT_SECONDS)) as client:
            resp = await client.get(f"{settings.ECOMMERCE_API_BASE}{path}", params=params, headers=headers)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(502, f"电商接口调用失败（{path}）：{exc}")
    if resp.status_code != 200:
        raise ToolError(502, f"电商接口返回异常状态码 {resp.status_code}（{path}），请检查 ECOMMERCE_TOKEN 与服务状态")
    payload = resp.json()
    # 电商信封实为 {code:0, msg, data}（0=成功），兼容 {ok:true,data} 两种形态
    success = payload.get("ok") is True or payload.get("code") == 0
    if not success:
        err = payload.get("error") or payload.get("msg") or payload
        raise ToolError(502, f"电商接口业务失败（{path}）：{err}")
    return payload.get("data")


async def _screen_summary(args: dict) -> dict:
    """ecommerce.screen.summary：数据大屏汇总（只读 + 溯源）。"""
    data = await _get_data("/api/v1/screen/summary")
    return {"source": "ecommerce-api", "endpoint": "/api/v1/screen/summary", "fetched_at": _now_iso(), "data": data}


async def _finance_bills(args: dict) -> dict:
    """ecommerce.finance.bills：账单分页列表（只读 + 溯源，默认 page=1&size=20）。"""
    params = {"page": int(args.get("page") or 1), "size": int(args.get("size") or 20)}
    data = await _get_data("/api/v1/finance/bills", params=params)
    return {"source": "ecommerce-api", "endpoint": "/api/v1/finance/bills", "fetched_at": _now_iso(), "page": params["page"], "size": params["size"], "data": data}


async def register_if_enabled() -> None:
    """条件注册入口：未启用（开关关 / token 空）或探测不可达 → 跳过并说明，绝不影响独立启动。"""
    if not settings.ECOMMERCE_ENABLED or not settings.ECOMMERCE_TOKEN.strip():
        logger.info("电商桥接未启用（ECOMMERCE_ENABLED=false 或 ECOMMERCE_TOKEN 为空），本次启动仅注册内置工具")
        return
    if not await _probe():
        logger.warning("电商服务不可达（%s），跳过注册 ecommerce.* 桥接工具；office-agent 独立运行不受影响（可选插件红线）", settings.ECOMMERCE_API_BASE)
        return
    registry.register(ToolSpec(
        name="ecommerce.screen.summary",
        description="读取电商客服系统数据大屏汇总（HTTP 桥接，只读，响应带 source/fetched_at 溯源）",
        scope=SCOPE_READ,
        needs_approval=False,
        schema={"type": "object", "properties": {}},
        handler=_screen_summary,
    ))
    registry.register(ToolSpec(
        name="ecommerce.finance.bills",
        description="读取电商客服系统账单分页列表（HTTP 桥接，只读，默认 page=1&size=20，响应带 source/fetched_at 溯源）",
        scope=SCOPE_READ,
        needs_approval=False,
        schema={
            "type": "object",
            "properties": {
                "page": {"type": "integer", "default": 1, "description": "页码"},
                "size": {"type": "integer", "default": 20, "description": "每页条数"},
            },
        },
        handler=_finance_bills,
    ))
    logger.info("电商桥接工具注册完成：ecommerce.screen.summary / ecommerce.finance.bills → %s", settings.ECOMMERCE_API_BASE)
