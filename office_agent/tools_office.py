"""内置办公工具包（领域无关，独立模式即可用）。

职责：注册三个演示级内置工具——
① office.report.generate：标题 + 指标字典 → 结构化中文日报。纯模板直出、不调任何大模型，
   每个数字后强带「(来源: input.metrics)」溯源标注，数值由输入原值直出，数值一致率天然 100%
   （对齐「数值不可编造、缺数宁可留白不补数」红线）；
② office.bi.query：白名单意图枚举 {demo_metrics, sales_trend} → 返回内置演示数据集，
   响应必含 source:"builtin-demo" 显式标注演示来源，绝不冒充真实外部数据；
③ office.memo.submit：起草并发布公告（写动作演示，scope=office:write + needs_approval=True）——
   写动作无直接生效通道，invoke 只落审批单，由其他账号审批通过后才真正执行
   （对齐「写动作必须经审批中心」红线）。
链路：main.lifespan → register_all() → registry；executor 经 ToolSpec.handler(args) 调用。
对齐：docs/office-agent仓库骨架与内核提取方案.md §4（tools-office 读层：report.generate / bi.query）。
"""

import copy
from datetime import datetime, timezone

from office_agent import registry
from office_agent.contracts import SCOPE_READ, SCOPE_WRITE, ToolError, ToolSpec

_PROVENANCE = "(来源: input.metrics)"

# 内置演示数据集（无 PII、无外部数据源；key 为白名单意图）
_DEMO_DATASETS: dict[str, dict] = {
    "demo_metrics": {
        "指标": [
            {"name": "本周活跃用户", "value": 328, "unit": "人"},
            {"name": "已处理文档", "value": 1206, "unit": "份"},
            {"name": "待办完成率", "value": 0.92, "unit": "比例"},
            {"name": "会议时长合计", "value": 46.5, "unit": "小时"},
        ],
        "说明": "内置演示数据集，仅用于独立模式演示，无任何真实外部数据源。",
    },
    "sales_trend": {
        "periods": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],
        "values": [120, 135, 128, 160, 175, 143, 96],
        "说明": "内置演示数据集（周度趋势演示），仅用于独立模式演示。",
    },
}
_INTENT_HINT = " / ".join(_DEMO_DATASETS)


def _now_text() -> str:
    # 显式 UTC（DTZ005 口径）：溯源时间戳必须可跨时区比对，本地时区只在展示层转换
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def _report_generate(args: dict) -> dict:
    """office.report.generate：结构化中文日报，纯模板直出，每个数字带溯源标注。"""
    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolError(400, "参数 title 不能为空：请传入日报标题")
    metrics = args.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        raise ToolError(400, '参数 metrics 必须为非空对象，形如 {"指标名": 数值}')

    lines: list[str] = [
        f"# {title}",
        "",
        f"生成时间：{_now_text()}（纯模板直出，未调用任何大模型）",
        "",
        "## 一、核心指标",
    ]
    count = 0
    for key, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError(400, f"指标「{key}」的值必须是数字（当前类型 {type(value).__name__}）")
        count += 1
        lines.append(f"- {key}：{value}{_PROVENANCE}")
    lines += [
        "",
        "## 二、小结",
        f"本日报共汇总 {count} 项指标{_PROVENANCE}；全部数值由输入原值直出、未经任何模型改写（口径：数值不可编造，缺数宁可留白不补数）。",
    ]
    return {
        "title": title,
        "report": "\n".join(lines),
        "metric_count": count,
        "numeric_consistency": "100%",
    }


async def _bi_query(args: dict) -> dict:
    """office.bi.query：白名单意图 → 内置演示数据集（响应显式标注演示来源）。"""
    intent = str(args.get("intent") or "").strip()
    if not intent:
        raise ToolError(400, f"参数 intent 不能为空，白名单可选值：{_INTENT_HINT}")
    dataset = _DEMO_DATASETS.get(intent)
    if dataset is None:
        raise ToolError(400, f"不支持的意图「{intent}」，白名单可选值：{_INTENT_HINT}")
    return {"intent": intent, "source": "builtin-demo", "data": copy.deepcopy(dataset)}


async def _memo_submit(args: dict) -> dict:
    """office.memo.submit：发布公告（写动作，仅在审批通过后由 decide 路径真正执行）。

    内容由入参原值直出（数值不编造红线）；演示实现无外部副作用，发布即确认回执。
    """
    title = str(args.get("title") or "").strip()
    content = str(args.get("content") or "").strip()
    if not title:
        raise ToolError(400, "参数 title 不能为空：请传入公告标题")
    if not content:
        raise ToolError(400, "参数 content 不能为空：请传入公告正文")
    return {
        "title": title,
        "content": content,
        "published": True,
        "published_at": _now_text(),
        "note": "演示实现：公告内容原值回执，无外部副作用；真实接入时在此落公告存储/通知渠道",
    }


def register_all() -> None:
    """把内置办公工具注册进注册中心（启动期调用；重名会抛 ToolError 直接暴露问题）。"""
    registry.register(
        ToolSpec(
            name="office.report.generate",
            description="生成结构化中文日报：输入标题与指标字典，纯模板直出（不调大模型），每个数字带 input.metrics 溯源标注，数值一致率 100%",
            scope=SCOPE_READ,
            needs_approval=False,
            schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "日报标题"},
                    "metrics": {
                        "type": "object",
                        "description": '指标字典：{"指标名": 数值}',
                        "additionalProperties": {"type": "number"},
                    },
                },
                "required": ["title", "metrics"],
            },
            handler=_report_generate,
        )
    )
    registry.register(
        ToolSpec(
            name="office.bi.query",
            description="办公 BI 查询（演示）：按白名单意图返回内置演示数据集，响应显式标注 source=builtin-demo",
            scope=SCOPE_READ,
            needs_approval=False,
            schema={
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "enum": ["demo_metrics", "sales_trend"],
                        "description": "查询意图（白名单枚举）",
                    }
                },
                "required": ["intent"],
            },
            handler=_bi_query,
        )
    )
    registry.register(
        ToolSpec(
            name="office.memo.submit",
            description="起草并发布公告（写动作演示）：需审批，invoke 只落审批单，审批通过后由复核人触发执行",
            scope=SCOPE_WRITE,
            needs_approval=True,
            schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "公告标题"},
                    "content": {"type": "string", "description": "公告正文"},
                },
                "required": ["title", "content"],
            },
            handler=_memo_submit,
        )
    )
