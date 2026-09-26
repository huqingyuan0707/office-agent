"""PRD §2.6 企业知识库增强检索单测（handler 直调，不起 HTTP 服务）。

覆盖：kb.ask 条目级权限过滤（visibility）+ KB_DIR 首行指令解析 +
      office.kb.search_unified 五源联查与身份过滤 +
      office.image.ask 引擎缺失降级与识别问答 + 三工具只读免审注册口径。
固件隔离：monkeypatch settings.DOCS_DIR/KB_DIR 到 tmp_path；
默认钉住「未配向量检索」零网络（与 test_kb_ask 同口径）。
对齐：AGENTS.md §3（权限适配/降级不 500/溯源）；
      智能办公Agent 产品需求文档.md §2.6。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_core.settings import settings
from office_agent_tools_office import (
    affairs,
    approval_submit,
    image_ask,
    kb,
    kb_unified,
    ocr,
)

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read", "office:write"])
CTX_HR = ToolContext(tenant="t1", username="carol", roles=["office:read", "hr"])
CTX_ADMIN = ToolContext(tenant="t1", username="admin", roles=["*"])
CTX_BOB = ToolContext(tenant="t1", username="bob", roles=["office:read", "office:write"])
CTX_APPROVER = ToolContext(
    tenant="t1", username="reviewer", roles=["admin", "approver", "office:read"]
)


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """文档/知识双目录锁进临时沙箱（每例独立，消除真实盘残留）。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    monkeypatch.setattr(settings, "DOCS_DIR", str(docs))
    monkeypatch.setattr(settings, "KB_DIR", str(kb_dir))
    return tmp_path


@pytest.fixture(autouse=True)
def _no_embedding_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认钉住「未配向量检索/向量库」：不给 EMBEDDING_* 且 MILVUS_URI 为空，零网络走字符检索。"""
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "")
    monkeypatch.setattr(settings, "MILVUS_URI", "")


# ---------------- visibility 原语 ----------------


def test_can_view_public_and_privileged() -> None:
    assert kb.can_view("public", []) is True
    assert kb.can_view("hr", ["hr"]) is True
    assert kb.can_view("hr", ["office:read"]) is False
    assert kb.can_view("hr", ["*"]) is True
    assert kb.can_view("hr", ["admin"]) is True
    assert kb.can_view("unknown-role-xyz", ["office:read"]) is False


def test_parse_visibility_block_directive() -> None:
    visibility, body = kb.parse_visibility_block("visibility: hr\n# 薪酬\n正文")
    assert visibility == "hr"
    assert "visibility" not in body
    assert "正文" in body


def test_parse_visibility_block_chinese_colon_and_default() -> None:
    visibility, _ = kb.parse_visibility_block("visibility：Finance\n正文")
    assert visibility == "finance"
    visibility, body = kb.parse_visibility_block("# 普通制度\n正文")
    assert visibility == "public"
    assert "普通制度" in body


# ---------------- kb.ask 权限过滤 ----------------


async def test_kb_ask_hides_restricted_from_normal(sandbox: Path) -> None:
    data = await kb._kb_ask(CTX, {"query": "薪酬", "top_k": 10})
    titles = [item["title"] for item in data["results"]]
    assert all("薪酬" not in title for title in titles)
    assert data["permission_filtered"] >= 1


async def test_kb_ask_shows_restricted_to_hr(sandbox: Path) -> None:
    data = await kb._kb_ask(CTX_HR, {"query": "薪酬", "top_k": 10})
    assert any("薪酬" in item["title"] for item in data["results"])
    assert data["permission_filtered"] == 0


async def test_kb_ask_admin_sees_all(sandbox: Path) -> None:
    data = await kb._kb_ask(CTX_ADMIN, {"query": "薪酬", "top_k": 10})
    assert any("薪酬" in item["title"] for item in data["results"])


async def test_kb_ask_file_directive_filters(sandbox: Path) -> None:
    kb_dir = Path(settings.KB_DIR)
    (kb_dir / "salary.md").write_text("visibility: hr\n# 薪档\n仅人力资源可见。", encoding="utf-8")
    (kb_dir / "notice.md").write_text("# 通知\n全员可见。", encoding="utf-8")
    hidden = await kb._kb_ask(CTX, {"query": "薪档", "top_k": 10})
    assert all(item["source"] != "local-kb:salary.md" for item in hidden["results"])
    assert hidden["permission_filtered"] >= 1
    shown = await kb._kb_ask(CTX_HR, {"query": "薪档", "top_k": 10})
    assert any(item["source"] == "local-kb:salary.md" for item in shown["results"])


async def test_kb_ask_new_builtins_cover_hr_admin_compliance(sandbox: Path) -> None:
    for query, keyword in (("试用期", "人事"), ("用印", "行政"), ("泄密", "合规")):
        data = await kb._kb_ask(CTX, {"query": query, "top_k": 3})
        assert data["count"] >= 1
        assert keyword in data["results"][0]["title"]


# ---------------- 跨源联合检索 ----------------


async def test_unified_queries_knowledge_only(sandbox: Path) -> None:
    data = await kb_unified._search_unified(
        CTX, {"query": "报销超过1000元需要谁审批", "sources": ["knowledge"]}
    )
    assert data["per_source"]["knowledge"]["count"] >= 1
    assert data["merged_count"] >= 1
    assert data["merged"][0]["origin"] == "knowledge"
    assert data["retrieval_mode"] == "bigram"
    assert data["degraded"] is False


async def test_unified_docs_source_hits_local_file(sandbox: Path) -> None:
    docs = Path(settings.DOCS_DIR)
    (docs / "sop.txt").write_text(
        "部署流程\n先备份数据库再发布新版本，回滚保留上一版镜像。", encoding="utf-8"
    )
    data = await kb_unified._search_unified(CTX, {"query": "部署流程", "sources": ["docs"]})
    assert data["per_source"]["docs"]["count"] >= 1
    assert "sop.txt" in data["per_source"]["docs"]["results"][0]["source"]


async def test_unified_docs_restricted_prefix_filters(sandbox: Path) -> None:
    docs = Path(settings.DOCS_DIR)
    (docs / "restricted-pay.txt").write_text("薪档明细\n仅限受限角色。", encoding="utf-8")
    hidden = await kb_unified._search_unified(CTX, {"query": "薪档明细", "sources": ["docs"]})
    assert hidden["per_source"]["docs"]["count"] == 0
    assert hidden["permission_filtered"] >= 1
    shown = await kb_unified._search_unified(CTX_ADMIN, {"query": "薪档明细", "sources": ["docs"]})
    assert shown["per_source"]["docs"]["count"] >= 1


async def test_unified_affairs_only_sees_own_todo(sandbox: Path) -> None:
    await affairs.add_todo("t1", "alice", title="交周报")
    await affairs.add_todo("t1", "bob", title="修水管")
    mine = await kb_unified._search_unified(CTX, {"query": "交周报", "sources": ["affairs"]})
    assert mine["per_source"]["affairs"]["count"] >= 1
    assert "交周报" in mine["merged"][0]["snippet"]
    other = await kb_unified._search_unified(CTX, {"query": "修水管", "sources": ["affairs"]})
    assert other["per_source"]["affairs"]["count"] == 0
    assert other["degraded"] is True


async def test_unified_approvals_identity_filter(sandbox: Path) -> None:
    await approval_submit._approval_submit(
        CTX,
        {
            "kind": "leave",
            "fields": {
                "leave_type": "年假",
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "reason": "休息",
            },
            "idem_key": "kb26-test-key-001",
        },
    )
    mine = await kb_unified._search_unified(CTX, {"query": "年假", "sources": ["approvals"]})
    assert mine["per_source"]["approvals"]["count"] >= 1
    stranger = await kb_unified._search_unified(
        CTX_BOB, {"query": "年假", "sources": ["approvals"]}
    )
    assert stranger["per_source"]["approvals"]["count"] == 0
    reviewer = await kb_unified._search_unified(
        CTX_APPROVER, {"query": "年假", "sources": ["approvals"]}
    )
    assert reviewer["per_source"]["approvals"]["count"] >= 1


async def test_unified_data_source_hits_demo_ledger(sandbox: Path) -> None:
    data = await kb_unified._search_unified(CTX, {"query": "官网改版", "sources": ["data"]})
    assert data["per_source"]["data"]["count"] >= 1
    assert data["merged"][0]["origin"] == "data"


async def test_unified_rejects_bad_args(sandbox: Path) -> None:
    with pytest.raises(BusinessError):
        await kb_unified._search_unified(CTX, {"query": "  "})
    with pytest.raises(BusinessError):
        await kb_unified._search_unified(CTX, {"query": "报销", "top_k": 99})
    with pytest.raises(BusinessError):
        await kb_unified._search_unified(CTX, {"query": "报销", "sources": ["nope"]})


# ---------------- 图片问答 ----------------


def _make_image(docs: Path, name: str) -> Path:
    target = docs / name
    Image.new("RGB", (120, 40), "white").save(target)
    return target


async def test_image_ask_degrades_without_engine(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = Path(settings.DOCS_DIR)
    _make_image(docs, "shot.png")
    monkeypatch.setattr(ocr, "ocr_available", lambda: (False, "pytesseract 未安装"))
    data = await image_ask._image_ask(CTX, {"file_path": "shot.png", "query": "图里有什么"})
    assert data["degraded"] is True
    assert data["retrieval_mode"] == "none"
    assert data["image"]["width"] == 120
    assert "pytesseract" in data["degraded_reason"]


async def test_image_ask_answers_from_ocr_text(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = Path(settings.DOCS_DIR)
    _make_image(docs, "alarm.png")

    class _FakeTess:
        @staticmethod
        def image_to_string(path: str, lang: str = "") -> str:
            _ = (path, lang)
            return "报错码 E502 网关超时\n请联系运维处理"

    monkeypatch.setattr(ocr, "pytesseract", _FakeTess)
    monkeypatch.setattr(ocr, "ocr_available", lambda: (True, ""))
    data = await image_ask._image_ask(CTX, {"file_path": "alarm.png", "query": "报错码是什么"})
    assert data["degraded"] is False
    assert data["count"] >= 1
    assert "E502" in data["answer_fragments"][0]["snippet"]
    assert data["retrieval_mode"] == "bigram"


async def test_image_ask_rejects_bad_args(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = Path(settings.DOCS_DIR)
    _make_image(docs, "shot.png")
    monkeypatch.setattr(ocr, "ocr_available", lambda: (False, "no engine"))
    with pytest.raises(BusinessError) as missing:
        await image_ask._image_ask(CTX, {"file_path": "gone.png", "query": "图里有什么"})
    assert missing.value.code == 1004
    with pytest.raises(BusinessError) as traversal:
        await image_ask._image_ask(CTX, {"file_path": "../evil.png", "query": "图里有什么"})
    assert traversal.value.code == 1001
    with pytest.raises(BusinessError):
        await image_ask._image_ask(CTX, {"file_path": "shot.png", "query": "  "})


# ---------------- 注册口径 ----------------


def test_new_tools_register_read_only() -> None:
    unified = {spec.name: spec for spec in kb_unified.specs()}
    assert set(unified) == {"office.kb.search_unified"}
    assert unified["office.kb.search_unified"].scope == "office:read"
    assert unified["office.kb.search_unified"].requires_approval is False
    asking = {spec.name: spec for spec in image_ask.specs()}
    assert set(asking) == {"office.image.ask"}
    assert asking["office.image.ask"].scope == "office:read"
    assert asking["office.image.ask"].requires_approval is False
