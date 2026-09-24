"""数据自助分析工具单元测试（handler 直调，不起 HTTP 服务）。

覆盖：台账查询（内置行 + 等值过滤 + limit + 未知数据集拒绝）、数值分析（统计/趋势/异常/
      空序列与非数字拒绝）、文本导出（markdown/csv 形状 + 缺列 unfilled + 非法格式拒绝）。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.4（V1.1 数据自助分析）。
"""

from __future__ import annotations

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_tools_office import data_analysis

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read"])


# ---------------- 台账查询 ----------------


async def test_query_returns_builtin_rows_with_provenance() -> None:
    data = await data_analysis._data_query(CTX, {"dataset": "sales"})
    assert data["count"] == 4
    assert data["total"] == 4
    assert data["source"] == "builtin-demo"
    assert data["fetched_at"]
    assert data["overlay_applied"] is False


async def test_query_filters_match_person() -> None:
    data = await data_analysis._data_query(CTX, {"dataset": "work", "filters": {"person": "张三"}})
    assert data["count"] == 2
    assert all(row["person"] == "张三" for row in data["rows"])


async def test_query_limit_truncates() -> None:
    data = await data_analysis._data_query(CTX, {"dataset": "work", "limit": 2})
    assert data["count"] == 2
    assert data["total"] == 5


async def test_query_rejects_unknown_dataset() -> None:
    try:
        await data_analysis._data_query(CTX, {"dataset": "finance"})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("未知数据集应被 1001 拒绝")


async def test_query_rejects_bad_limit() -> None:
    try:
        await data_analysis._data_query(CTX, {"dataset": "sales", "limit": 0})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("非法 limit 应被 1001 拒绝")


# ---------------- 数值分析 ----------------


async def test_analyze_stats_and_trend() -> None:
    data = await data_analysis._data_analyze(CTX, {"label": "业绩", "values": [100, 120, 140]})
    assert data["count"] == 3
    assert data["sum"] == 360
    assert abs(data["avg"] - 120.0) < 1e-9
    assert data["min"] == 100
    assert data["max"] == 140
    assert data["trend"]["direction"] == "上升"
    assert data["numeric_consistency"] == "100%"
    assert "业绩" in data["brief"]


async def test_analyze_marks_outlier_beyond_two_sigma() -> None:
    values = [10, 10, 10, 10, 10, 10, 10, 10, 10, 100]
    data = await data_analysis._data_analyze(CTX, {"values": values})
    assert len(data["anomalies"]) == 1
    assert data["anomalies"][0] == {"index": 9, "value": 100.0}


async def test_analyze_rejects_empty_values() -> None:
    try:
        await data_analysis._data_analyze(CTX, {"values": []})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("空序列应被 1001 拒绝")


async def test_analyze_rejects_non_numeric() -> None:
    try:
        await data_analysis._data_analyze(CTX, {"values": [1, "很多"]})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("非数字元素应被 1001 拒绝")


# ---------------- 文本导出 ----------------


async def test_export_markdown_table_shape() -> None:
    data = await data_analysis._data_export(
        CTX,
        {
            "title": "业绩导出",
            "format": "markdown",
            "columns": ["person", "amount"],
            "rows": [{"person": "张三", "amount": 135000}],
        },
    )
    assert "# 业绩导出" in data["content"]
    assert "| person | amount |" in data["content"]
    assert "135000" in data["content"]
    assert data["row_count"] == 1
    assert data["unfilled"] == []


async def test_export_csv_and_unfilled() -> None:
    data = await data_analysis._data_export(
        CTX,
        {
            "title": "考勤",
            "format": "csv",
            "columns": ["person", "status"],
            "rows": [{"person": "张三"}, {"person": "李四", "status": "迟到"}],
        },
    )
    lines = data["content"].splitlines()
    assert lines[0] == "person,status"
    assert lines[1] == "张三,"
    assert data["unfilled"] == ["status"]


async def test_export_rejects_bad_format() -> None:
    try:
        await data_analysis._data_export(
            CTX, {"title": "x", "format": "pdf", "columns": ["a"], "rows": []}
        )
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("非法格式应被 1001 拒绝")


# ---------------- 注册聚合 ----------------


def test_specs_register_three_read_tools() -> None:
    specs = {spec.name: spec for spec in data_analysis.specs()}
    assert set(specs) == {"office.data.query", "office.data.analyze", "office.data.export"}
    assert all(spec.scope == "office:read" for spec in specs.values())
    assert all(spec.requires_approval is False for spec in specs.values())
