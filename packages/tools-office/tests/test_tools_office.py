"""tools-office 单元测试（handler 直调，不起 HTTP 服务）。

覆盖：日报/周报模板直出与参数红线、会议纪要、kb.ask 检索与降级、ocr.image 元数据与降级、
      路径守卫防穿越。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §5.1。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_core.settings import settings
from office_agent_tools_office import kb, ocr, tools

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read", "office:write"])


# ---------------- 日报 / 周报 ----------------


async def test_report_daily_keeps_baseline() -> None:
    data = await tools._report_generate(CTX, {"title": "日报", "metrics": {"成交额": 100}})
    assert data["report_type"] == "daily"
    assert "成交额：100(来源: input.metrics)" in data["report"]
    assert data["numeric_consistency"] == "100%"
    assert "本周亮点" not in data["report"]


async def test_report_weekly_adds_sections() -> None:
    data = await tools._report_generate(
        CTX,
        {
            "title": "周报",
            "report_type": "weekly",
            "metrics": {"pv": 12},
            "highlights": ["上线新首页"],
            "next_plan": ["筹备发布会"],
        },
    )
    assert "报告类型：周报" in data["report"]
    assert "- 上线新首页" in data["report"]
    assert "- 筹备发布会" in data["report"]


async def test_report_weekly_missing_sections_blank() -> None:
    data = await tools._report_generate(
        CTX, {"title": "周报", "report_type": "weekly", "metrics": {"pv": 1}}
    )
    assert "未提供，留白不编造" in data["report"]


async def test_report_rejects_non_numeric_metric() -> None:
    with pytest.raises(BusinessError):
        await tools._report_generate(CTX, {"title": "日报", "metrics": {"bad": "很多"}})


async def test_minutes_generates_sections() -> None:
    data = await tools._minutes_generate(
        CTX,
        {
            "title": "周会",
            "attendees": ["张三", "李四"],
            "agenda": ["进度对齐"],
            "decisions": ["周五封版"],
            "action_items": [{"task": "发周报", "owner": "张三", "due": "周五"}],
        },
    )
    assert data["counts"] == {"attendee": 2, "agenda": 1, "decision": 1, "action_item": 1}
    assert "责任人：张三｜期限：周五" in data["minutes"]


async def test_minutes_blank_when_missing() -> None:
    data = await tools._minutes_generate(CTX, {"title": "临时会"})
    assert "未提供参会人名单" in data["minutes"]
    assert "未提供行动项" in data["minutes"]


async def test_minutes_action_item_requires_task() -> None:
    with pytest.raises(BusinessError):
        await tools._minutes_generate(CTX, {"title": "周会", "action_items": [{"owner": "张三"}]})


# ---------------- 知识库问答 ----------------


async def test_kb_ask_hits_builtin_entry() -> None:
    data = await kb._kb_ask(CTX, {"query": "报销超过1000元需要谁审批"})
    assert data["count"] >= 1
    assert data["results"][0]["source"] == "builtin-demo"
    assert "报销" in data["results"][0]["title"] or "报销" in data["results"][0]["snippet"]


async def test_kb_ask_no_hit_degrades_without_fabrication() -> None:
    data = await kb._kb_ask(CTX, {"query": "zzzqxj999"})
    assert data["degraded"] is True
    assert data["results"] == []
    assert data["degraded_reason"]


async def test_kb_ask_rejects_bad_top_k() -> None:
    with pytest.raises(BusinessError):
        await kb._kb_ask(CTX, {"query": "考勤", "top_k": 99})
    with pytest.raises(BusinessError):
        await kb._kb_ask(CTX, {"query": ""})


async def test_kb_ask_reads_local_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    (kb_dir / "sop.md").write_text(
        "# 会议室预定\n通过日历工具预定后自动锁会议室。", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "KB_DIR", str(kb_dir))
    data = await kb._kb_ask(CTX, {"query": "会议室怎么预定", "top_k": 1})
    assert data["results"][0]["source"] == "local-kb:sop.md"
    assert data["kb_dir_missing"] is False


# ---------------- 图片 OCR ----------------


def _make_image(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    Image.new("RGB", (60, 30), color=(240, 240, 240)).save(path)
    return path


async def test_ocr_returns_real_metadata_and_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    _make_image(tmp_path, "shot.png")
    data = await ocr._ocr_image(CTX, {"file_path": "shot.png"})
    assert data["image"]["width"] == 60
    assert data["image"]["format"] == "PNG"
    assert data["source"] == "local-docs:shot.png"
    assert data["text"] == ""  # 引擎缺失不编造识别文本
    assert data["degraded"] is True
    assert "Tesseract" in data["degraded_reason"] or "pytesseract" in data["degraded_reason"]


async def test_ocr_rejects_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    with pytest.raises(BusinessError):
        await ocr._ocr_image(CTX, {"file_path": "../secret.png"})
    with pytest.raises(BusinessError):
        await ocr._ocr_image(CTX, {"file_path": "note.txt"})


async def test_ocr_missing_file_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    with pytest.raises(BusinessError) as excinfo:
        await ocr._ocr_image(CTX, {"file_path": "absent.png"})
    assert excinfo.value.http_status == 404


# ---------------- 注册清单 ----------------


def test_specs_contain_v1_tools() -> None:
    names = {spec.name for spec in (*tools.specs(), *kb.specs(), *ocr.specs())}
    assert {"office.report.generate", "office.minutes.generate", "kb.ask", "ocr.image"} <= names
