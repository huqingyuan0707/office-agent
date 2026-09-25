"""图表自动解读（office.data.chart_insight，对齐 PRD §2.4 新增「图表自动解读，标注指标异动」）。

职责（office:read，免审批）：
- 入参 categories（分类标签）+ values（对应数值）：等长 1-50，数值原值直出不编造；
- 出参四件套：stats（合计/均值/最值 + 占比冠军）、trend（首尾方向）、highlights
  （最高/最低/均值±2σ 异常逐条标注到分类名）、brief（中文解读 Markdown）、
  chart_svg（同数据横向条形图 SVG，异常下标红，浏览器直接可渲染）。

实现说明：图片走 SVG 纯文本（stdlib 拼接，零第三方依赖）——服务端不出位图，
      刻意避开 matplotlib + 中文字体双依赖（私有化环境字体缺失会出方块乱码）；
      SVG 文本由浏览器用系统字体渲染，中文不乱码。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：分类与数值长度不一致/空序列/非数字一律 1001 中文拒绝（不臆造补齐）；
      占比分母为 0 时如实记「—」不除零；数值只取入参原值。
对齐：AGENTS.md §3（降级绝不 500/数值不编造）；智能办公Agent 产品需求文档.md §2.4
      （智能分析：异常识别 + 结论简报；输出导出：图片格式——SVG 形态）。
"""

from __future__ import annotations

import logging
import math
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.registry import register

from .data_export import _series_or_raise, render_chart_svg

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"


async def _chart_insight(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.data.chart_insight：分类数值序列的统计 + 异动标注 + 中文解读 + SVG 图。"""
    _ = ctx
    title = str(args.get("title") or "").strip() or "图表解读"
    labels, numbers = _series_or_raise(args.get("categories"), args.get("values"))
    count = len(numbers)
    total = math.fsum(numbers)
    avg = total / count
    top = max(range(count), key=lambda i: numbers[i])
    bottom = min(range(count), key=lambda i: numbers[i])
    shares = [(value / total * 100) if total != 0 else None for value in numbers]
    if count >= 2 and numbers[0] != numbers[-1]:
        direction = "上升" if numbers[-1] > numbers[0] else "下降"
    elif count >= 2:
        direction = "持平"
    else:
        direction = "样本不足"
    variance = math.fsum((value - avg) ** 2 for value in numbers) / count
    sigma = math.sqrt(variance)
    anomaly_idx = {
        index for index in range(count) if sigma > 0 and abs(numbers[index] - avg) > 2 * sigma
    }

    def _reason(base: str, index: int) -> str:
        """最高/最低若同时是 2σ 异常，理由如实叠加（不丢任一标注）。"""
        if index in anomaly_idx:
            return f"{base}（异常：偏离均值超过 2σ，σ={sigma:.4f}）"
        return base

    highlights: list[dict[str, Any]] = [
        {
            "index": top,
            "category": labels[top],
            "value": numbers[top],
            "share_pct": shares[top],
            "reason": _reason("最高", top),
        },
        {
            "index": bottom,
            "category": labels[bottom],
            "value": numbers[bottom],
            "share_pct": shares[bottom],
            "reason": _reason("最低", bottom),
        },
    ]
    for index in sorted(anomaly_idx):
        if index in (top, bottom):
            continue
        highlights.append(
            {
                "index": index,
                "category": labels[index],
                "value": numbers[index],
                "share_pct": shares[index],
                "reason": f"异常：偏离均值超过 2σ（σ={sigma:.4f}）",
            }
        )
    share_text = f"{shares[top]:.2f}%" if shares[top] is not None else "—（合计为 0）"
    lines = [
        f"# {title}",
        f"共 {count} 类，合计 {total}，均值 {avg:.4f}；首尾趋势{direction}。",
        f"最高：{labels[top]}（{numbers[top]}，占比 {share_text}）；"
        f"最低：{labels[bottom]}（{numbers[bottom]}）。",
    ]
    if anomaly_idx:
        names = "、".join(f"{labels[i]}（{numbers[i]}）" for i in sorted(anomaly_idx))
        lines.append(f"异动 {len(anomaly_idx)} 处（口径：偏离均值超过 2σ）：{names}。")
    else:
        lines.append("无异动（全部落在均值±2σ 内）。")
    return {
        "title": title,
        "count": count,
        "sum": total,
        "avg": avg,
        "trend": {"direction": direction},
        "highlights": highlights,
        "anomaly_count": len(anomaly_idx),
        "brief": "\n".join(lines),
        "chart_svg": render_chart_svg(title, labels, numbers, anomaly_idx),
        "svg_mime": "image/svg+xml",
        "numeric_consistency": "100%",
        "source": "input.categories+values",
    }


def specs() -> tuple[ToolSpec, ...]:
    """图表解读工具的 ToolSpec（读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.data.chart_insight",
            scope=SCOPE_READ,
            description="图表自动解读：分类数值序列的统计 + 最高/最低/2σ异常标注到分类名 + "
            "中文解读简报 + 同数据 SVG 条形图（异常标红），数值全部原值直出不编造",
            params={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "图表标题（默认 图表解读）"},
                    "categories": {
                        "type": "array",
                        "description": "分类标签数组（与 values 等长，1-50 个）",
                        "items": {"type": "string"},
                    },
                    "values": {
                        "type": "array",
                        "description": "对应数值数组（非空，元素全是数字）",
                        "items": {"type": "number"},
                    },
                },
                "required": ["categories", "values"],
                "additionalProperties": False,
            },
            handler=_chart_insight,
        ),
    )


def register_all() -> list[str]:
    """注册图表解读工具；返回已注册工具名列表。"""
    return [register(spec).name for spec in specs()]
