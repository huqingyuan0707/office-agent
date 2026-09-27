"""任务控制台结果导出渲染（md/csv/xlsx → 字节内容，不触盘）。

职责：任务基本信息 + input/output/error 原文（缺省留空，绝不编造）；
      openpyxl 可选依赖缺失时给可操作提示（降级绝不 500）。
对齐：AGENTS.md §3（降级绝不 500）；产品口径「结果交付，能一键导出」。
"""

from __future__ import annotations

import csv as _csv
import io
import json
from typing import Any

from office_agent_core.errors import BusinessError, ErrorCode

#: 导出格式白名单（md/csv/xlsx 之外如实拒绝，不伪造 PDF/PPTX/SVG）
EXPORT_FORMATS = ("md", "csv", "xlsx")

MIME_BY_FORMAT = {
    "md": "text/markdown",
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _render_md(view: dict[str, Any]) -> bytes:
    lines = [
        f"# 任务：{view['name']}",
        "",
        f"- 任务 ID：{view['id']}",
        f"- 状态：{view['status_label']}",
        f"- 来源：{view['source_label']}",
        f"- 负责人：{view['username']}",
        f"- 创建时间：{view['created_at']}",
        f"- 更新时间：{view['updated_at']}",
        "",
        "## 输入",
        "",
        "```json",
        json.dumps(view.get("input") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 输出",
        "",
        "```json",
        json.dumps(view.get("output") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    if view.get("error"):
        lines += [
            "## 错误信息",
            "",
            "```json",
            json.dumps(view["error"], ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    return "\n".join(lines).encode("utf-8")


def _render_csv(view: dict[str, Any]) -> bytes:
    buffer = io.StringIO()
    writer = _csv.writer(buffer, lineterminator="\n")
    writer.writerow(["任务ID", "名称", "状态", "来源", "负责人", "进度", "创建时间", "备注"])
    writer.writerow(
        [
            view["id"],
            view["name"],
            view["status_label"],
            view["source_label"],
            view["username"],
            f"{view['progress']:.0f}",
            view["created_at"],
            str(view.get("error_hint") or ""),
        ]
    )
    return buffer.getvalue().encode("utf-8")


def _render_xlsx(view: dict[str, Any]) -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font as _Font
    except ImportError:
        raise BusinessError(
            ErrorCode.TOOL_CALL_FAILED,
            "当前环境未安装 openpyxl，无法导出 Excel（请执行 pip install openpyxl 后重试）",
        ) from None
    title = str(view["name"])
    book = Workbook()
    sheet = book.active
    sheet.title = (title[:24] or "任务导出").replace("[", "_").replace("]", "_")[:31]
    sheet.append(["字段", "内容"])
    for key in (
        "id",
        "name",
        "status_label",
        "source_label",
        "username",
        "progress",
        "created_at",
        "updated_at",
        "error_hint",
    ):
        sheet.append([key, str(view.get(key) or "")])
    if view.get("input"):
        sheet.append(
            ["input(JSON)", json.dumps(view["input"], ensure_ascii=False, indent=2, default=str)]
        )
    if view.get("output"):
        sheet.append(
            ["output(JSON)", json.dumps(view["output"], ensure_ascii=False, indent=2, default=str)]
        )
    sheet["A1"].font = _Font(bold=True)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def render_export(fmt: str, view: dict[str, Any]) -> bytes:
    """按格式渲染字节内容（md/csv 文本 UTF-8，xlsx 二进制）。"""
    if fmt == "md":
        return _render_md(view)
    if fmt == "csv":
        return _render_csv(view)
    return _render_xlsx(view)
