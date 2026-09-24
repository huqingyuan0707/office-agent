"""tools-ecommerce 插件：把上游电商系统的工具纳入 office-agent 注册中心（读 5 + 写 1）

链路：server lifespan → plugins.load_plugin_tools() → 本模块 register()
      → 声明远程 ToolSpec → executor 执行时经 linkage 层出站到上游 agent-gateway。

红线：
- 本插件是 office-agent 的**可选插件**：office-agent 没有它照样独立运行（主包零业务分支）；
- 上游地址与凭据只来自环境变量 ``LINKAGE_PROVIDERS``，本文件既不写地址也不写令牌；
- 提供方未配置时**不注册**这些工具——宁可在清单里缺席，也不注册一个注定调不通的工具；
- 入参 Schema 与上游 connectors 逐字对齐，保证上游 ``validate_args`` 一次通过。

写边界（联动方案 §7.2 模式②回流）：
- ticket.create 是第一个回流写工具：requires_approval=True 恒送审，invoke 只落审批单，
  复核员批准后 decide_approval 以申请人身份重放原 args（idem_key 随 args 出站到上游）；
- 上游以 (tenant, idem_key) 唯一约束做幂等回放——同键重放返回原单，审批重试/重发绝不双单；
- 审批闸门唯一在本侧，上游网关只认 Scope + 幂等键，两级职责不重叠。
"""

from __future__ import annotations

import json
import logging

from office_agent_core import linkage, registry
from office_agent_core.contracts import RemoteBinding, ToolSpec

logger = logging.getLogger(__name__)

#: 上游提供方标识（与 LINKAGE_PROVIDERS 里的键一致；地址与令牌在那里配）
PROVIDER_ID = "ecommerce"


def _remote(tool: str) -> RemoteBinding:
    """远程绑定：本工具的实现在上游，内核只认「哪个提供方 + 上游的哪个工具名」。"""
    return RemoteBinding(provider_id=PROVIDER_ID, tool=tool)


_ORDER_ID = {
    "type": "string",
    "title": "订单号",
    "minLength": 1,
    "maxLength": 40,
}


def specs() -> tuple[ToolSpec, ...]:
    """只读工具规格（与上游 connectors 的 params 逐字对齐）。"""
    return (
        ToolSpec(
            name="order.query",
            scope="order:read",
            description="查询订单状态、明细、金额与售后记录（必校验订单归属）",
            params={
                "type": "object",
                "properties": {"order_id": _ORDER_ID},
                "required": ["order_id"],
                "additionalProperties": False,
            },
            remote=_remote("order.query"),
        ),
        ToolSpec(
            name="logistics.query",
            scope="order:read",
            description="查询订单物流轨迹与预计到达（节点卡片数据源）",
            params={
                "type": "object",
                "properties": {"order_id": _ORDER_ID},
                "required": ["order_id"],
                "additionalProperties": False,
            },
            remote=_remote("logistics.query"),
        ),
        ToolSpec(
            name="stock.query",
            scope="stock:read",
            description="按 SKU 查询可用库存（可用 = 在库 − 预占 − 锁定，可再按尺码过滤）",
            params={
                "type": "object",
                "properties": {
                    "sku_id": {
                        "type": "string",
                        "title": "SKU ID",
                        "minLength": 1,
                        "maxLength": 40,
                    },
                    "size": {"type": "string", "title": "尺码", "maxLength": 20},
                },
                "required": ["sku_id"],
                "additionalProperties": False,
            },
            remote=_remote("stock.query"),
        ),
        ToolSpec(
            name="coupon.query",
            scope="promo:read",
            description="查询本租户在售优惠活动与剩余预算（只读，发券须走审批）",
            params={
                "type": "object",
                "properties": {"only_active": {"type": "boolean", "title": "只看进行中"}},
                "additionalProperties": False,
            },
            remote=_remote("coupon.query"),
        ),
        ToolSpec(
            name="kb.retrieve",
            scope="kb:read",
            description="检索企业知识库（唯一权威来源，带密级/生效期/渠道治理过滤）",
            params={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "title": "检索问题",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "top_k": {"type": "integer", "title": "召回条数", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            remote=_remote("kb.retrieve"),
        ),
        ToolSpec(
            name="ticket.create",
            scope="ticket:write",
            description=(
                "回流创建电商协同工单（联动模式②：本侧恒送审，复核员批准后才出站执行；"
                "idem_key 必填，上游同键幂等回放，重放返回原单绝不双单）"
            ),
            params={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "title": "工单类型",
                        "minLength": 1,
                        "maxLength": 32,
                    },
                    "source_ref": {"type": "string", "title": "来源关联", "maxLength": 64},
                    "assignee": {"type": "string", "title": "处理人", "maxLength": 64},
                    "sla_hours": {
                        "type": "integer",
                        "title": "SLA 小时",
                        "minimum": 1,
                        "maximum": 720,
                    },
                    "idem_key": {
                        "type": "string",
                        "title": "幂等键",
                        "minLength": 8,
                        "maxLength": 64,
                    },
                },
                "required": ["kind", "idem_key"],
                "additionalProperties": False,
            },
            remote=_remote("ticket.create"),
            idempotent=True,
            requires_approval=True,
            approval_action="ticket.create",
        ),
    )


def _config_hint() -> str:
    """未配置时的可照做提示（模板里的值都是占位，不含任何真实凭据）。"""
    template = {PROVIDER_ID: {"base_url": "http://<上游地址:端口>", "token": "<服务账号令牌>"}}
    return json.dumps(template, ensure_ascii=False)


def register() -> list[str]:
    """注册只读工具（幂等，重复调用只覆盖同规格项）；提供方未配置则一个都不注册。"""
    if not linkage.configured(PROVIDER_ID):
        logger.warning(
            "上游提供方 %s 未配置，本次不注册其工具；在环境变量 LINKAGE_PROVIDERS 中补上即可：%s",
            PROVIDER_ID,
            _config_hint(),
        )
        return []
    return [registry.register(spec).name for spec in specs()]
