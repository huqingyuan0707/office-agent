"""内部术语库翻译单测（handler 直调，不起 HTTP 服务）。

覆盖：中→英/英→中统一替换与计数、单遍替换无乒乓回写、CSV 叠加覆盖内置表、
      零命中原样返回、空文本拒绝、注册口径。
固件隔离：monkeypatch settings.DOCS_DIR 到 tmp_path，仓库 data 目录零污染。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.1（术语翻译）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_core.settings import settings
from office_agent_tools_office import terms

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read"])


@pytest.fixture()
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """测试隔离的文档工作目录（术语 CSV 经 settings.DOCS_DIR 实时读取）。"""
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    return tmp_path


async def test_translate_zh_to_en_with_counts(docs_dir: Path) -> None:
    """中→英统一替换并计数（无 CSV 叠加时 source 为内置表）。"""
    _ = docs_dir
    data = await terms._terms_translate(CTX, {"text": "请查收日报，审批已通过。"})
    assert data["translated"] == "请查收daily report，approval已通过。"
    assert data["replacement_count"] == 2
    assert data["source"] == "builtin-terms"
    assert data["overlay_applied"] is False


async def test_translate_no_pingpong_rewrite(docs_dir: Path) -> None:
    """单遍替换：刚换上的译文不被反向对换回去（计数诚实）。"""
    _ = docs_dir
    data = await terms._terms_translate(CTX, {"text": "日报"})
    assert data["translated"] == "daily report"
    assert data["replacement_count"] == 1


async def test_translate_csv_overlay_overrides_builtin(docs_dir: Path) -> None:
    """CSV 叠加：同源词覆盖内置表并标注来源。"""
    (docs_dir / "terms.csv").write_text("source,target\n日报,day-report\n", encoding="utf-8")
    data = await terms._terms_translate(CTX, {"text": "日报"})
    assert data["translated"] == "day-report"
    assert data["overlay_applied"] is True
    assert data["source"] == "builtin-terms+local-csv"


async def test_translate_no_hit_passthrough(docs_dir: Path) -> None:
    """零命中原样返回（不编造译文）。"""
    _ = docs_dir
    data = await terms._terms_translate(CTX, {"text": "今天天气不错。"})
    assert data["translated"] == "今天天气不错。"
    assert data["replacements"] == []
    assert "原样返回" in data["note"]


async def test_translate_rejects_empty(docs_dir: Path) -> None:
    """空文本拒绝。"""
    _ = docs_dir
    try:
        await terms._terms_translate(CTX, {"text": "  "})
    except BusinessError as exc:
        assert exc.code == 1001
    else:
        raise AssertionError("空文本应被 1001 拒绝")


def test_terms_registers_read_tool() -> None:
    """注册口径：读 scope、免审批。"""
    specs = {spec.name: spec for spec in terms.specs()}
    assert set(specs) == {"office.terms.translate"}
    assert specs["office.terms.translate"].scope == "office:read"
    assert specs["office.terms.translate"].requires_approval is False
