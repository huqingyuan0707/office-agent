"""数据导出渲染器（office.data.export / chart_insight 的多格式后端，对齐 PRD §2.4 新增「多格式导出」）。

职责（纯函数，无工具注册）：
- render_markdown_table / render_csv_text：表格文本渲染（由 data_analysis 迁入，
  同一行为只允许一个实现，收口到此处）；
- render_workbook_base64：columns + rows 直出 .xlsx 二进制 → base64 文本
  （JSON 信封只能走文本，base64 是「读口径不写盘」与二进制交付的合规折中；
  缺值留空，绝不填假值）；
- render_chart_svg：categories + values 直出横向条形图 SVG（纯 stdlib，
  零第三方依赖；数值原值直出，highlight 下标标红用于异动标注）。

链路：data_analysis._data_export（excel 分支）/ data_insight._chart_insight（svg）
       → 本模块渲染 → handler 包信封返回。
红线：openpyxl 缺失时 excel 抛 1001 可操作提示（绝不 500）；SVG 中文标签
      XML 转义；文件名 hint 去路径分隔符。
对齐：AGENTS.md §3（降级绝不 500/数值不编造）；智能办公Agent 产品需求文档.md §2.4
      （输出导出：Excel/图片/Markdown 多格式）。
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from office_agent_core.errors import BusinessError, ErrorCode

try:  # 可选依赖：缺库只影响 excel 分支（抛可操作提示），不阻断 markdown/csv/svg
    from openpyxl import Workbook
except ImportError:  # pragma: no cover
    Workbook = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MAX_SERIES = 50


def render_markdown_table(title: str, columns: list[str], rows: list[dict[str, Any]]) -> str:
    """渲染 markdown 表格（缺值留空，绝不填假值）。"""
    lines = [f"# {title}", "", "| " + " | ".join(columns) + " |"]
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in columns) + " |")
    return "\n".join(lines)


def render_csv_text(columns: list[str], rows: list[dict[str, Any]]) -> str:
    """渲染 csv 文本（缺值留空；换行符统一 \\n）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(col, "") for col in columns])
    return buffer.getvalue()


def safe_filename_hint(title: str, suffix: str) -> str:
    """文件名 hint：去路径分隔符与首尾空白（只做下载建议，不触盘）。"""
    cleaned = str(title or "导出").strip().replace("/", "_").replace("\\", "_") or "导出"
    return f"{cleaned[:60]}{suffix}"


def render_workbook_base64(title: str, columns: list[str], rows: list[dict[str, Any]]) -> bytes:
    """表格直出 .xlsx 二进制（表头 + 数据行；缺值留空单元格）。

    openpyxl 缺失抛 1001 可操作提示（调用方按依赖缺失如实转达，不静默降级成假表）。
    """
    if Workbook is None:
        raise BusinessError(
            ErrorCode.TOOL_CALL_FAILED,
            "当前环境未安装 openpyxl，无法导出 Excel（请执行 pip install openpyxl 后重试）",
        )
    book = Workbook()
    sheet = book.active
    sheet.title = safe_filename_hint(title, "")[:31].replace("[", "_").replace("]", "_")
    sheet.append(list(columns))
    for row in rows:
        sheet.append([row.get(col) for col in columns])
    for index, col in enumerate(columns, 1):
        width = max([len(str(col))] + [len(str(row.get(col, ""))) for row in rows])
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = min(
            width + 2, 40
        )
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _series_or_raise(categories: Any, values: Any) -> tuple[list[str], list[float]]:
    """图表序列口径：等长非空（1-50），分类非空文本，数值全是数字（bool 拒绝）。"""
    if not isinstance(categories, list) or not categories:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 categories 必须是非空数组（分类标签）")
    if not isinstance(values, list) or not values:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 values 必须是非空数组（对应数值）")
    if len(categories) != len(values):
        raise BusinessError(
            ErrorCode.PARAM_INVALID,
            f"参数 categories 与 values 长度必须一致（当前 {len(categories)} vs {len(values)}）",
        )
    if len(categories) > _MAX_SERIES:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"图表序列最多 {_MAX_SERIES} 组（当前 {len(categories)}）"
        )
    labels = [str(item).strip() for item in categories]
    if any(not label for label in labels):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 categories 不能含空标签")
    numbers: list[float] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"参数 values 元素必须是数字（当前类型 {type(item).__name__}）",
            )
        numbers.append(float(item))
    return labels, numbers


def render_chart_svg(
    title: str, categories: list[str], values: list[float], highlight: set[int] | None = None
) -> str:
    """横向条形图 SVG（纯字符串拼接；highlight 下标标红，其余统一蓝色）。

    缩放口径：(值-最小值)/(极差) 等比；全相等时满格（表达「持平」而非零宽消失）。
    长标签截断显示 + <title> 保留全称（悬停可见，不丢信息）。
    """
    hot = highlight or set()
    width, left, right, top, bar_h, gap = 640, 150, 90, 54, 24, 10
    lowest, highest = min(values), max(values)
    span = highest - lowest
    plot_w = width - left - right
    height = top + len(values) * (bar_h + gap) + 30
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        f"<title>{_xml_escape(title)}</title>",
        f'<text x="16" y="30" font-size="16" font-weight="bold">{_xml_escape(title)}</text>',
    ]
    for index, (label, value) in enumerate(zip(categories, values, strict=True)):
        ratio = 1.0 if span == 0 else (value - lowest) / span
        bar_w = max(int(ratio * plot_w), 2)
        pos_y = top + index * (bar_h + gap)
        fill = "#d93025" if index in hot else "#2f7cf6"
        shown = label if len(label) <= 10 else label[:10] + "…"
        parts.append(
            f'<text x="14" y="{pos_y + 17}" font-size="13">{_xml_escape(shown)}'
            f"<title>{_xml_escape(label)}</title></text>"
        )
        parts.append(
            f'<rect x="{left}" y="{pos_y}" width="{bar_w}" height="{bar_h}" '
            f'fill="{fill}" rx="3"><title>{_xml_escape(label)}：{value}</title></rect>'
        )
        parts.append(f'<text x="{left + bar_w + 8}" y="{pos_y + 17}" font-size="13">{value}</text>')
    parts.append(
        f'<text x="16" y="{height - 8}" font-size="12" fill="#666">'
        f"红色为异动标注；数值全部为入参原值（n={len(values)}）</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts)
