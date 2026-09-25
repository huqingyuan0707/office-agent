"""PRD §2.4 新增三件单元测试（handler 直调，不起 HTTP 服务）。

覆盖：常用查询保存/清单/一句话复用/删除（含租户隔离、同名拒绝、穿越拒绝）、
      export excel 分支（base64 可解回 xlsx 行列）、chart_insight（最高/最低/
      2σ 异动标注 + SVG + 口径拒绝）。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.4。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from openpyxl import load_workbook

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_core.settings import settings
from office_agent_tools_office import data_analysis, data_export, data_insight, data_saved

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read", "office:write"])
OTHER = ToolContext(tenant="t2", username="bob", roles=["office:read"])


@pytest.fixture
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """测试隔离的文档工作目录（存档不污染真实 DOCS_DIR）。"""
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    return tmp_path


def _save_args(name: str = "月度业绩") -> dict:
    return {
        "name": name,
        "dataset": "sales",
        "filters": {"person": "张三"},
        "limit": 10,
        "description": "看张三业绩",
        "idem_key": "idem-0001",
    }


# ---------------- 常用查询保存与复用 ----------------


async def test_saved_roundtrip_save_list_run_delete(docs_dir: Path) -> None:
    saved = await data_saved._query_save(CTX, _save_args())
    assert saved["name"] == "月度业绩"
    assert saved["file"] == "data_queries/月度业绩.json"

    listed = await data_saved._query_list(CTX, {})
    assert listed["count"] == 1
    assert listed["queries"][0]["dataset"] == "sales"
    assert listed["queries"][0]["filters"] == {"person": "张三"}

    result = await data_saved._query_run(CTX, {"name": "月度业绩"})
    assert result["saved_as"] == "月度业绩"
    assert result["count"] == 2  # 存档口径实时重查：张三两行
    assert all(row["person"] == "张三" for row in result["rows"])

    deleted = await data_saved._query_delete(CTX, {"name": "月度业绩"})
    assert deleted["deleted"] is True
    assert (await data_saved._query_list(CTX, {}))["count"] == 0
    assert docs_dir  # 固件占位（消除未使用告警）


async def test_saved_rejects_duplicate_name(docs_dir: Path) -> None:
    _ = docs_dir
    await data_saved._query_save(CTX, _save_args())
    try:
        await data_saved._query_save(CTX, _save_args())
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("同名重复保存应被 1001 拒绝")


async def test_saved_run_missing_is_404(docs_dir: Path) -> None:
    _ = docs_dir
    try:
        await data_saved._query_run(CTX, {"name": "不存在的查询"})
    except BusinessError as exc:
        assert exc.code == 1004
    else:
        raise AssertionError("复用不存在的查询应 1004")


async def test_saved_is_tenant_isolated(docs_dir: Path) -> None:
    _ = docs_dir
    await data_saved._query_save(CTX, _save_args())
    assert (await data_saved._query_list(OTHER, {}))["count"] == 0
    try:
        await data_saved._query_run(OTHER, {"name": "月度业绩"})
    except BusinessError as exc:
        assert exc.code == 1004
    else:
        raise AssertionError("跨租户复用应 1004（不泄露存在性）")


async def test_saved_rejects_traversal_and_bad_dataset(docs_dir: Path) -> None:
    _ = docs_dir
    for bad in ("../evil", "a/b", ""):
        try:
            await data_saved._query_save(CTX, {**_save_args(), "name": bad})
        except BusinessError as exc:
            assert exc.code == 1001
        else:
            raise AssertionError(f"非法查询名 {bad!r} 应被 1001 拒绝")
    try:
        await data_saved._query_save(CTX, {**_save_args(), "dataset": "finance"})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("未知数据集应被 1001 拒绝")


# ---------------- export excel 分支 ----------------


async def test_export_excel_base64_roundtrip() -> None:
    data = await data_analysis._data_export(
        CTX,
        {
            "title": "业绩导出",
            "format": "excel",
            "columns": ["person", "amount"],
            "rows": [{"person": "张三", "amount": 135000}, {"person": "李四"}],
        },
    )
    assert data["encoding"] == "base64"
    assert data["filename_hint"].endswith(".xlsx")
    assert data["unfilled"] == ["amount"]
    raw = base64.b64decode(data["content"])
    assert raw[:2] == b"PK"  # xlsx 是 zip 包
    sheet = load_workbook(io.BytesIO(raw)).active
    assert [cell.value for cell in sheet[1]] == ["person", "amount"]
    assert sheet["B2"].value == 135000
    assert sheet["B3"].value in (None, "")


def test_safe_filename_hint_strips_separators() -> None:
    assert "/" not in data_export.safe_filename_hint("a/b\\c", ".xlsx")
    assert data_export.safe_filename_hint("", ".xlsx").endswith(".xlsx")


# ---------------- 图表解读 ----------------


async def test_insight_marks_top_bottom_and_svg() -> None:
    data = await data_insight._chart_insight(
        CTX,
        {
            "title": "Q3 业绩",
            "categories": ["张三", "李四", "王五"],
            "values": [135000, 110000, 90000],
        },
    )
    reasons = {item["category"]: item["reason"] for item in data["highlights"]}
    assert reasons["张三"] == "最高"
    assert reasons["王五"] == "最低"
    assert data["anomaly_count"] == 0
    assert "最高：张三" in data["brief"]
    assert "无异动" in data["brief"]
    assert data["chart_svg"].lstrip().startswith("<svg")
    assert "李四" in data["chart_svg"]
    assert data["numeric_consistency"] == "100%"


async def test_insight_flags_two_sigma_anomaly_with_label() -> None:
    cats = [f"门店{i}" for i in range(10)]
    data = await data_insight._chart_insight(
        CTX, {"categories": cats, "values": [10, 10, 10, 10, 10, 10, 10, 10, 10, 100]}
    )
    assert data["anomaly_count"] == 1
    flagged = [item for item in data["highlights"] if "异常" in item["reason"]]
    assert len(flagged) == 1 and flagged[0]["category"] == "门店9"
    assert "异动 1 处" in data["brief"]
    assert 'fill="#d93025"' in data["chart_svg"]  # 异常下标红


async def test_insight_rejects_bad_series() -> None:
    bad_args = [
        {"categories": ["a"], "values": [1, 2]},  # 等长违反
        {"categories": [], "values": []},  # 空序列
        {"categories": ["a"], "values": ["很多"]},  # 非数字
        {"categories": [""], "values": [1]},  # 空标签
    ]
    for args in bad_args:
        try:
            await data_insight._chart_insight(CTX, args)
        except BusinessError as exc:
            assert exc.code == 1001
        else:
            raise AssertionError(f"非法序列 {args} 应被 1001 拒绝")


# ---------------- 注册口径 ----------------


def test_specs_new_tools_scopes_and_approval() -> None:
    saved = {spec.name: spec for spec in data_saved.specs()}
    assert set(saved) == {
        "office.data.query.save",
        "office.data.query.list",
        "office.data.query.run",
        "office.data.query.delete",
    }
    assert saved["office.data.query.save"].requires_approval is True
    assert saved["office.data.query.delete"].requires_approval is True
    assert saved["office.data.query.list"].requires_approval is False
    assert saved["office.data.query.run"].requires_approval is False
    insight = {spec.name: spec for spec in data_insight.specs()}
    assert set(insight) == {"office.data.chart_insight"}
    assert insight["office.data.chart_insight"].scope == "office:read"
