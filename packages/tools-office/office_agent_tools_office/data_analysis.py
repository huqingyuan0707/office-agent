"""数据自助分析工具（office.data.query / analyze / export，对齐 PRD §2.4 V1.1 首项）。

职责：
- office.data.query（office:read）：按数据集查本地演示台账（项目/工时/业绩/考勤四类内置
  + DOCS_DIR/data/{dataset}.csv 可选叠加），等值过滤 + limit，带 source + fetched_at 溯源；
- office.data.analyze（office:read）：数值序列统计（个数/求和/均值/最值）+ 首尾趋势 +
  均值±2σ 异常标记 + 中文结论简报，数值只取入参原值，空序列直接 1001 拒绝（不编造）；
- office.data.export（office:read）：columns + rows 渲染 markdown/csv 文本（不写盘，
  落盘由调用方决定），缺值留空并在 unfilled 如实列出。

链路：__init__.register_all() → registry.register(spec) → executor.call 执行 handler。
红线：三工具全是读口径（免审批）；纯本地实现，不触及 ORM / FastAPI；CSV 叠加读失败
      只降级不阻断，绝不 500。
对齐：AGENTS.md §3（分层红线/降级绝不 500/溯源）；智能办公Agent 产品需求文档.md §2.4
      （自然语言取数/智能分析/输出导出——保存常用查询后置，见模块末尾说明）。
"""

from __future__ import annotations

import asyncio
import copy
import csv
import io
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from office_agent_core.contracts import ToolContext, ToolSpec
from office_agent_core.errors import BusinessError, ErrorCode
from office_agent_core.registry import register
from office_agent_core.settings import settings

logger = logging.getLogger(__name__)

SCOPE_READ = "office:read"

_MAX_LIMIT = 100
_MAX_VALUES = 500
_MAX_ROWS = 500
_MAX_COLUMNS = 30

#: 内置演示台账（source=builtin-demo；真实部署用 DOCS_DIR/data/{dataset}.csv 叠加/替换行）
_PROJECT_ROWS: list[dict[str, Any]] = [
    {"name": "官网改版", "owner": "产品经理", "status": "进行中", "progress": 60},
    {"name": "移动端适配", "owner": "前端工程师", "status": "未开始", "progress": 0},
    {"name": "数据看板", "owner": "后端工程师", "status": "已完成", "progress": 100},
    {"name": "客服知识库", "owner": "产品经理", "status": "进行中", "progress": 30},
]
_WORK_ROWS: list[dict[str, Any]] = [
    {"person": "张三", "date": "2026-09-21", "hours": 8, "task": "需求评审"},
    {"person": "张三", "date": "2026-09-22", "hours": 6, "task": "原型设计"},
    {"person": "李四", "date": "2026-09-21", "hours": 8, "task": "接口开发"},
    {"person": "李四", "date": "2026-09-22", "hours": 9, "task": "联调"},
    {"person": "王五", "date": "2026-09-22", "hours": 4, "task": "用例编写"},
]
_SALES_ROWS: list[dict[str, Any]] = [
    {"person": "张三", "month": "2026-08", "amount": 120000},
    {"person": "张三", "month": "2026-09", "amount": 135000},
    {"person": "李四", "month": "2026-08", "amount": 98000},
    {"person": "李四", "month": "2026-09", "amount": 110000},
]
_ATTENDANCE_ROWS: list[dict[str, Any]] = [
    {"person": "张三", "date": "2026-09-22", "status": "正常"},
    {"person": "李四", "date": "2026-09-22", "status": "迟到"},
    {"person": "王五", "date": "2026-09-22", "status": "请假"},
]

_DATASETS: dict[str, list[dict[str, Any]]] = {
    "project": _PROJECT_ROWS,
    "work": _WORK_ROWS,
    "sales": _SALES_ROWS,
    "attendance": _ATTENDANCE_ROWS,
}
_DATASET_LABELS: dict[str, str] = {
    "project": "项目台账",
    "work": "工时台账",
    "sales": "业绩台账",
    "attendance": "考勤台账",
}


def _now_text() -> str:
    """显式 UTC：溯源时间戳必须可跨时区比对。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _dataset_or_raise(dataset: str) -> str:
    """数据集口径校验：只认四类内置名（未知名中文可操作报错，不静默空查）。"""
    name = str(dataset or "").strip()
    if name not in _DATASETS:
        valid = "、".join(sorted(_DATASETS))
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 dataset 只能是 {valid}（当前：{dataset}）"
        )
    return name


def _load_csv_overlay(dataset: str) -> tuple[list[dict[str, Any]], bool]:
    """读 DOCS_DIR/data/{dataset}.csv 叠加行；文件缺席/损坏只降级（返回空叠加）。"""
    path = Path(settings.DOCS_DIR) / "data" / f"{dataset}.csv"
    if not path.is_file():
        return [], False
    try:
        if path.stat().st_size > 200_000:
            logger.warning("台账叠加文件过大已跳过：%s", path.name)
            return [], False
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        logger.warning("台账叠加文件不可读已跳过 %s：%s", path.name, str(exc)[:120])
        return [], False
    try:
        rows = [dict(row) for row in csv.DictReader(io.StringIO(text))]
    except csv.Error as exc:
        logger.warning("台账叠加文件解析失败已跳过 %s：%s", path.name, str(exc)[:120])
        return [], False
    return [row for row in rows if row], True


def _match_filters(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    """等值过滤：按字符串比对（缺键即不命中，不过滤出幻影行）。"""
    for key, value in filters.items():
        if key not in row or str(row[key]) != str(value):
            return False
    return True


async def _data_query(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.data.query：查演示台账 + CSV 叠加，等值过滤 + limit，带溯源。"""
    _ = ctx
    dataset = _dataset_or_raise(args.get("dataset"))
    filters = args.get("filters") or {}
    if not isinstance(filters, dict):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 filters 必须是键值对对象")
    limit = args.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 limit 必须是 1-{_MAX_LIMIT} 的整数")
    base = copy.deepcopy(_DATASETS[dataset])
    overlay, applied = await asyncio.to_thread(_load_csv_overlay, dataset)
    all_rows = base + overlay
    matched = [row for row in all_rows if _match_filters(row, filters)][:limit]
    source = "builtin-demo" + ("+local-csv" if applied else "")
    return {
        "dataset": dataset,
        "dataset_label": _DATASET_LABELS[dataset],
        "rows": matched,
        "count": len(matched),
        "total": len(all_rows),
        "filters": filters,
        "overlay_applied": applied,
        "source": source,
        "fetched_at": _now_text(),
    }


def _numbers_or_raise(values: Any) -> list[float]:
    """数值序列口径：非空数组 + 元素全是数字（bool 拒绝，避免 true 被当 1）。"""
    if not isinstance(values, list) or not values:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 values 必须是非空数组")
    if len(values) > _MAX_VALUES:
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 values 最多 {_MAX_VALUES} 个（当前 {len(values)}）"
        )
    numbers: list[float] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise BusinessError(
                ErrorCode.PARAM_INVALID,
                f"参数 values 元素必须是数字（当前类型 {type(item).__name__}）",
            )
        numbers.append(float(item))
    return numbers


async def _data_analyze(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.data.analyze：统计 + 趋势 + 异常标记 + 中文简报（数值全部原值直出）。"""
    _ = ctx
    label = str(args.get("label") or "").strip() or "数据分析"
    numbers = _numbers_or_raise(args.get("values"))
    count = len(numbers)
    total = math.fsum(numbers)
    avg = total / count
    minimum, maximum = min(numbers), max(numbers)
    if count >= 2 and numbers[0] != numbers[-1]:
        change = numbers[-1] - numbers[0]
        direction = "上升" if change > 0 else "下降"
        pct = (
            f"{change / abs(numbers[0]) * 100:.2f}%" if numbers[0] != 0 else "（基数 0，无法算比）"
        )
    elif count >= 2:
        direction, pct = "持平", "0.00%"
    else:
        direction, pct = "样本不足", "—"
    variance = math.fsum((value - avg) ** 2 for value in numbers) / count
    sigma = math.sqrt(variance)
    anomalies = [
        {"index": index, "value": numbers[index]}
        for index in range(count)
        if sigma > 0 and abs(numbers[index] - avg) > 2 * sigma
    ]
    brief = (
        f"# {label}\n共 {count} 个数据（来源: input.values），合计 {total}，"
        f"均值 {avg:.4f}，最小 {minimum}，最大 {maximum}；"
        f"首尾趋势{direction}（{pct}）；异常 {len(anomalies)} 个"
        f"（口径：偏离均值超过 2σ，σ={sigma:.4f}）。"
    )
    return {
        "label": label,
        "count": count,
        "sum": total,
        "avg": avg,
        "min": minimum,
        "max": maximum,
        "trend": {"direction": direction, "change_pct": pct},
        "anomalies": anomalies,
        "brief": brief,
        "numeric_consistency": "100%",
        "source": "input.values",
    }


def _columns_or_raise(columns: Any) -> list[str]:
    """列口径：非空字符串数组（空列名拒绝，不渲染幻影列）。"""
    if not isinstance(columns, list) or not columns:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 columns 必须是非空数组")
    if len(columns) > _MAX_COLUMNS:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 columns 最多 {_MAX_COLUMNS} 列")
    names = [str(col).strip() for col in columns]
    if any(not name for name in names):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 columns 不能含空列名")
    return names


def _rows_or_raise(rows: Any) -> list[dict[str, Any]]:
    """行口径：对象数组（非对象行拒绝，不臆造结构）。"""
    if not isinstance(rows, list):
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 rows 必须是对象数组")
    if len(rows) > _MAX_ROWS:
        raise BusinessError(ErrorCode.PARAM_INVALID, f"参数 rows 最多 {_MAX_ROWS} 行")
    for row in rows:
        if not isinstance(row, dict):
            raise BusinessError(ErrorCode.PARAM_INVALID, "参数 rows 元素必须是键值对对象")
    return list(rows)


def _render_markdown(title: str, columns: list[str], rows: list[dict[str, Any]]) -> str:
    """渲染 markdown 表格（缺值留空，绝不填假值）。"""
    lines = [f"# {title}", "", "| " + " | ".join(columns) + " |"]
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in columns) + " |")
    return "\n".join(lines)


def _render_csv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    """渲染 csv 文本（缺值留空；换行符统一 \\n）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(col, "") for col in columns])
    return buffer.getvalue()


async def _data_export(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """office.data.export：渲染导出文本（不写盘），缺列留空 + unfilled 如实列出。"""
    _ = ctx
    title = str(args.get("title") or "").strip()
    if not title:
        raise BusinessError(ErrorCode.PARAM_INVALID, "参数 title 不能为空：请传入导出标题")
    fmt = str(args.get("format") or "markdown").strip()
    if fmt not in ("markdown", "csv"):
        raise BusinessError(
            ErrorCode.PARAM_INVALID, f"参数 format 只能是 markdown/csv（当前：{fmt}）"
        )
    columns = _columns_or_raise(args.get("columns"))
    rows = _rows_or_raise(args.get("rows"))
    unfilled = [col for col in columns if any(col not in row for row in rows)]
    content = (
        _render_markdown(title, columns, rows) if fmt == "markdown" else _render_csv(columns, rows)
    )
    return {
        "title": title,
        "format": fmt,
        "content": content,
        "row_count": len(rows),
        "unfilled": unfilled,
        "note": "纯文本导出，未写盘；落盘由调用方决定（写盘动作走审批闸门）",
    }


def specs() -> tuple[ToolSpec, ...]:
    """三个数据分析工具的 ToolSpec（读口径，免审批）。"""
    return (
        ToolSpec(
            name="office.data.query",
            scope=SCOPE_READ,
            description="查询办公台账：项目/工时/业绩/考勤四类演示数据 + DOCS_DIR/data 下同名 CSV 可选叠加，等值过滤 + limit，带 source + fetched_at 溯源",
            params={
                "type": "object",
                "properties": {
                    "dataset": {
                        "type": "string",
                        "description": "数据集（project 项目 / work 工时 / sales 业绩 / attendance 考勤）",
                        "enum": ["project", "work", "sales", "attendance"],
                    },
                    "filters": {
                        "type": "object",
                        "description": "等值过滤条件（例：按 person=张三 过滤）",
                    },
                    "limit": {"type": "integer", "description": "返回行数上限（1-100，默认 20）"},
                },
                "required": ["dataset"],
                "additionalProperties": False,
            },
            handler=_data_query,
        ),
        ToolSpec(
            name="office.data.analyze",
            scope=SCOPE_READ,
            description="分析数值序列：个数/求和/均值/最值 + 首尾趋势 + 均值±2σ 异常标记 + 中文结论简报，数值全部原值直出不编造",
            params={
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "分析主题（默认 数据分析）"},
                    "values": {
                        "type": "array",
                        "description": "数值数组（非空，元素全是数字）",
                        "items": {"type": "number"},
                    },
                },
                "required": ["values"],
                "additionalProperties": False,
            },
            handler=_data_analyze,
        ),
        ToolSpec(
            name="office.data.export",
            scope=SCOPE_READ,
            description="导出 markdown/csv 文本：columns + rows 渲染（不写盘），缺值留空并在 unfilled 如实列出",
            params={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "导出标题", "minLength": 1},
                    "format": {
                        "type": "string",
                        "description": "导出格式（markdown/csv，默认 markdown）",
                        "enum": ["markdown", "csv"],
                    },
                    "columns": {
                        "type": "array",
                        "description": "列名数组（非空）",
                        "items": {"type": "string"},
                    },
                    "rows": {
                        "type": "array",
                        "description": "行对象数组",
                        "items": {"type": "object"},
                    },
                },
                "required": ["title", "columns", "rows"],
                "additionalProperties": False,
            },
            handler=_data_export,
        ),
    )


def register_all() -> list[str]:
    """注册三个数据分析工具；返回已注册工具名列表。

    后置说明：PRD §2.4 的「保存常用查询 / 一句话复用」需持久化写动作（恒送审 +
    落盘/落库），与 office.template.save 同口径，放在下一步单独做（本文件只收读口径，
    避免读写混单）。
    """
    return [register(spec).name for spec in specs()]
