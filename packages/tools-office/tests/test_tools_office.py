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
from office_agent_tools_office import doc_compare, kb, ocr, task_planner, templates, tools

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


# ---------------- 文档对比 ----------------


def _make_docx(path: Path, paragraphs: list[str]) -> None:
    import docx as docx_lib

    document = docx_lib.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    document.save(str(path))


async def test_doc_compare_reports_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    _make_docx(tmp_path / "a.docx", ["第一段", "第二段", "第三段"])
    _make_docx(tmp_path / "b.docx", ["第一段", "第二段改了", "第四段新增"])
    data = await doc_compare._doc_compare(CTX, {"file_a": "a.docx", "file_b": "b.docx"})
    assert data["counts"]["added"] == 1
    assert data["counts"]["removed"] == 1
    assert data["counts"]["changed"] == 1
    assert data["details"]["added"][0]["text"] == "第四段新增"
    assert data["details"]["removed"][0]["text"] == "第三段"
    assert "修改 1 段" in data["change_summary"]


async def test_doc_compare_identical_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    _make_docx(tmp_path / "same1.docx", ["只有一段"])
    _make_docx(tmp_path / "same2.docx", ["只有一段"])
    data = await doc_compare._doc_compare(CTX, {"file_a": "same1.docx", "file_b": "same2.docx"})
    assert data["counts"] == {
        "paragraph_a": 1,
        "paragraph_b": 1,
        "added": 0,
        "removed": 0,
        "changed": 0,
        "unchanged": 1,
    }


async def test_doc_compare_missing_file_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    _make_docx(tmp_path / "a.docx", ["第一段"])
    with pytest.raises(BusinessError) as excinfo:
        await doc_compare._doc_compare(CTX, {"file_a": "a.docx", "file_b": "ghost.docx"})
    assert excinfo.value.http_status == 404


async def test_doc_compare_rejects_non_docx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    with pytest.raises(BusinessError):
        await doc_compare._doc_compare(CTX, {"file_a": "a.txt", "file_b": "b.docx"})


# ---------------- 任务拆解 ----------------


async def test_task_decompose_with_full_info() -> None:
    data = await task_planner._task_decompose(
        CTX,
        {
            "goal": "官网改版项目，最终交付上线",
            "duration_weeks": 4,
            "participants": ["产品", "UI", "前端", "测试"],
        },
    )
    assert data["total"] == 5
    assert data["subtasks"][0]["owner"] == "产品"
    assert data["subtasks"][2]["owner"] == "前端"
    assert data["subtasks"][3]["owner"] == "测试"
    assert data["subtasks"][2]["dependencies"] == ["task-2"]
    assert all(item["due_date"] for item in data["subtasks"])
    assert data["missing_info"] == []


async def test_task_decompose_missing_participants_and_duration_never_fabricates() -> None:
    data = await task_planner._task_decompose(CTX, {"goal": "内部工具改造"})
    assert data["subtasks"][0]["owner"] == ""
    assert data["subtasks"][0]["due_date"] == ""
    assert any("参与人" in item for item in data["missing_info"])
    assert any("工期" in item for item in data["missing_info"])
    assert data["next_hint"].startswith("请核对")


async def test_task_decompose_unmatched_phase_left_blank() -> None:
    data = await task_planner._task_decompose(
        CTX, {"goal": "项目", "duration_weeks": 2, "participants": ["运营"]}
    )
    blank_titles = {item["title"] for item in data["subtasks"] if not item["owner"]}
    assert "开发与实施" in blank_titles
    assert any("未能从参与人中识别出责任人" in item for item in data["missing_info"])


async def test_task_decompose_rejects_bad_duration() -> None:
    with pytest.raises(BusinessError):
        await task_planner._task_decompose(CTX, {"goal": "项目", "duration_weeks": 99})
    with pytest.raises(BusinessError):
        await task_planner._task_decompose(CTX, {"goal": "项目", "start_date": "2026/09/01"})


async def test_task_commit_returns_receipts_with_notify_flags() -> None:
    data = await task_planner._task_commit(
        CTX,
        {
            "tasks": [
                {
                    "title": "需求调研",
                    "owner": "张三",
                    "due_date": "2026-10-09",
                    "priority": "high",
                },
                {"title": "自行跟进事项"},
            ],
            "idem_key": "decompose-0001",
        },
    )
    assert data["count"] == 2
    assert data["created"][0]["notified"] == "true"
    assert data["created"][1]["notified"] == "false"
    assert data["notified_owners"] == ["张三"]
    assert "不臆造接收人" in data["note"]


async def test_task_commit_requires_title_and_caps_size() -> None:
    with pytest.raises(BusinessError):
        await task_planner._task_commit(CTX, {"tasks": [{"owner": "张三"}], "idem_key": "k" * 8})
    with pytest.raises(BusinessError):
        await task_planner._task_commit(
            CTX, {"tasks": [{"title": f"t{i}"} for i in range(51)], "idem_key": "k" * 8}
        )


# ---------------- 自定义模板 ----------------


async def test_template_save_writes_json_and_apply_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    saved = await templates._template_save(
        CTX,
        {
            "name": "weekly-report",
            "title": "{姓名}的周报",
            "sections": ["本周完成：{本周工作}", "下周计划：{下周计划}"],
            "idem_key": "tpl-0000001",
        },
    )
    assert saved["published"] is True
    assert (tmp_path / "templates" / "weekly-report.json").exists()

    applied = await templates._template_apply(
        CTX, {"name": "weekly-report", "values": {"姓名": "李四", "本周工作": "发版"}}
    )
    assert applied["title"] == "李四的周报"
    assert applied["sections"][0] == "本周完成：发版"
    assert applied["unfilled"] == ["下周计划"]  # 未提供的占位符如实列出，不编造
    assert applied["source"] == "local-docs:templates/weekly-report.json"


async def test_template_apply_missing_template_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    with pytest.raises(BusinessError) as excinfo:
        await templates._template_apply(CTX, {"name": "ghost"})
    assert excinfo.value.http_status == 404


async def test_template_name_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    for bad in ("../evil", "a/b", "a\\b", ""):
        with pytest.raises(BusinessError):
            await templates._template_save(
                CTX, {"name": bad, "sections": ["段落"], "idem_key": "k" * 8}
            )


# ---------------- 注册清单 ----------------


def test_specs_contain_v1_tools() -> None:
    names = {
        spec.name
        for module in (tools, kb, ocr, doc_compare, task_planner, templates)
        for spec in module.specs()
    }
    assert {
        "office.report.generate",
        "office.minutes.generate",
        "kb.ask",
        "ocr.image",
        "office.doc.compare",
        "office.task.decompose",
        "office.task.commit",
        "office.template.save",
        "office.template.apply",
    } <= names
