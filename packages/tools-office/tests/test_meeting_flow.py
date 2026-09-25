"""会议全流程补充三工具单测（meeting_flow.py，handler 直调，不起 HTTP 服务）。

覆盖：会前资料包真实抽取与单文件失败如实标注/全失败 degraded/穿越名逐文件拒绝、
      会中速记三类要点归类与无命中 degraded、会后行动项×台账五态对账（业务当天判逾期）、
      入参校验、注册口径（全读免审）。
对齐：AGENTS.md §5（验证命令）；智能办公Agent 产品需求文档.md §2.5。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_core.settings import settings
from office_agent_tools_office import affairs, meeting_flow

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read"])


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """DOCS_DIR 锁进临时目录（资料文件与事务存储共用同一隔离）。"""
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    return tmp_path


# ---------------- 会前：整理会议资料 ----------------


async def test_materials_assembles_real_excerpt(store: Path) -> None:
    (store / "方案.txt").write_text(
        "第一段：官网改版方案背景与目标。\n第二段：预算与排期。", encoding="utf-8"
    )
    data = await meeting_flow._meeting_materials(
        CTX, {"title": "官网改版评审", "files": ["方案.txt"], "topics": ["方案走查"]}
    )
    assert data["counts"] == {"file_total": 1, "file_ok": 1}
    assert data["degraded"] is False
    assert "方案.txt" in data["pack"]
    assert "官网改版方案背景与目标" in data["pack"]
    assert "1. 方案走查" in data["pack"]
    assert data["source"].startswith("local-docs:")


async def test_materials_missing_file_marked_failed_without_500(store: Path) -> None:
    (store / "在场.txt").write_text("内容", encoding="utf-8")
    data = await meeting_flow._meeting_materials(
        CTX, {"title": "周会", "files": ["缺失.txt", "在场.txt", "../越界.txt"]}
    )
    states = {m["filename"]: m["status"] for m in data["materials"]}
    assert states["在场.txt"] == "ok"
    assert states["缺失.txt"] == "failed" and "文件不存在" in data["materials"][0]["reason"]
    assert data["counts"] == {"file_total": 3, "file_ok": 1}
    assert data["degraded"] is False


async def test_materials_all_failed_degrades_without_fabrication() -> None:
    data = await meeting_flow._meeting_materials(CTX, {"title": "周会", "files": ["无此文件.txt"]})
    assert data["degraded"] is True
    assert "不编造内容" in data["degraded_reason"]


async def test_materials_rejects_empty_inputs() -> None:
    with pytest.raises(BusinessError) as exc:
        await meeting_flow._meeting_materials(CTX, {"title": "周会", "files": []})
    assert exc.value.code == 1001


# ---------------- 会中：记录内容梳理要点 ----------------


async def test_digest_classifies_decision_action_risk() -> None:
    notes = "\n".join(
        [
            "决议：官网改版方案通过，进入排期。",
            "张三负责下周输出设计稿。",
            "联调出现阻塞，接口延期两天。",
            "闲聊：中午吃什么。",
        ]
    )
    data = await meeting_flow._meeting_digest(CTX, {"notes": notes, "title": "评审会"})
    assert data["counts"]["decision"] == 1
    assert data["counts"]["action"] == 1
    assert data["counts"]["risk"] == 1
    assert "决议：官网改版方案通过，进入排期。" in data["digest"]
    assert "闲聊" not in data["digest"]
    assert data["degraded"] is False


async def test_digest_no_marker_degrades() -> None:
    data = await meeting_flow._meeting_digest(CTX, {"notes": "今天天气不错\n大家随便聊聊"})
    assert data["degraded"] is True
    assert "不编造要点" in data["degraded_reason"]


async def test_digest_rejects_empty_notes() -> None:
    with pytest.raises(BusinessError) as exc:
        await meeting_flow._meeting_digest(CTX, {"notes": "  "})
    assert exc.value.code == 1001


# ---------------- 会后：行动项落实跟进 ----------------


async def test_followup_reports_five_states(store: Path) -> None:
    done = await affairs.add_todo("t1", "alice", title="交设计稿", due_date="2020-01-05")
    await affairs.update_todo("t1", "alice", done["id"], {"status": "done"})
    await affairs.add_todo("t1", "alice", title="补测试用例", due_date="2020-01-01")
    await affairs.add_todo("t1", "alice", title="整理复盘材料")
    gone = await affairs.add_todo("t1", "alice", title="取消的事项")
    await affairs.update_todo("t1", "alice", gone["id"], {"status": "cancelled"})
    data = await meeting_flow._meeting_followup(
        CTX,
        {
            "action_items": [
                {"task": "交设计稿", "owner": "张三"},
                {"task": "补测试用例", "owner": "李四", "due": "2020-01-01"},
                {"task": "整理复盘材料"},
                {"task": "取消的事项"},
                {"task": "从未建单的事项", "due": "2030-12-31"},
            ]
        },
    )
    states = {r["task"]: r["state"] for r in data["items"]}
    assert states == {
        "交设计稿": "done",
        "补测试用例": "overdue",
        "整理复盘材料": "open",
        "取消的事项": "cancelled",
        "从未建单的事项": "missing",
    }
    assert data["counts"] == {"done": 1, "overdue": 1, "open": 1, "cancelled": 1, "missing": 1}
    assert "未建单" in data["followup"]
    assert data["source"].startswith("local-affairs-store")


async def test_followup_missing_todo_keeps_input_due_and_owner(store: Path) -> None:
    data = await meeting_flow._meeting_followup(
        CTX, {"action_items": [{"task": "没建过的事", "owner": "王五", "due": "第2周周五"}]}
    )
    item = data["items"][0]
    assert (
        item["state_label"] == "未建单" and item["owner"] == "王五" and item["due"] == "第2周周五"
    )


async def test_followup_rejects_bad_items() -> None:
    with pytest.raises(BusinessError) as exc:
        await meeting_flow._meeting_followup(CTX, {"action_items": []})
    assert exc.value.code == 1001
    with pytest.raises(BusinessError) as exc:
        await meeting_flow._meeting_followup(CTX, {"action_items": [{"owner": "没写事项"}]})
    assert exc.value.code == 1001


# ---------------- 注册口径 ----------------


def test_specs_three_read_tools_no_approval() -> None:
    specs = {spec.name: spec for spec in meeting_flow.specs()}
    assert set(specs) == {
        "office.meeting.materials",
        "office.meeting.digest",
        "office.meeting.followup",
    }
    assert all(
        spec.scope == "office:read" and not spec.requires_approval for spec in specs.values()
    )
