"""文件内容问答（office.file.ask）单元测试（handler 直调，不起 HTTP 服务）。

覆盖：段落级命中与溯源、零命中/无文字层如实降级、向量语义通道与回退标注、
      缺文件 404 / 穿越拒绝 / 空问题拒绝 / top_k 越界拒绝、注册口径（只读免审）。
固件隔离：monkeypatch settings.DOCS_DIR 到 tmp_path；默认钉住「未配向量检索」零网络。
对齐：AGENTS.md §3（降级绝不 500）、§5（验证命令）；
      智能办公Agent 产品需求文档.md §2.1（文件解析：内容提取、解读与问答）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_core.settings import settings
from office_agent_tools_office import file_ask, retrieval

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read", "office:write"])

_DOC = """项目背景
本方案用于替换旧版报销系统，覆盖 2026 年 Q1 上线范围。

验收标准
单笔报销审批链路端到端耗时不超过 3 个工作日，发票识别准确率不低于 95%。

交付物
需求说明书、接口文档、上线培训材料各一份。
"""


@pytest.fixture()
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """测试隔离的文档工作目录（handler 经 settings.DOCS_DIR 实时读取）。"""
    monkeypatch.setattr(settings, "DOCS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _no_embedding_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认钉住「未配向量检索/向量库」：不给 EMBEDDING_* 且 MILVUS_URI 为空，零网络走字符检索。

    Milvus 必须一并钉空——本机 .env 一登记 MILVUS_URI，检索就会先试向量库打网络。
    """
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "")
    monkeypatch.setattr(settings, "MILVUS_URI", "")


# ---------------- 字符检索（默认通道） ----------------


async def test_file_ask_hits_paragraph(docs_dir: Path) -> None:
    """命中定位到具体段落（不返回整篇），带 source 溯源。"""
    (docs_dir / "plan.txt").write_text(_DOC, encoding="utf-8")
    data = await file_ask._file_ask(CTX, {"filename": "plan.txt", "query": "验收标准是什么"})
    assert data["count"] >= 1
    assert data["retrieval_mode"] == "bigram"
    assert data["degraded"] is False
    top = data["answer_fragments"][0]
    assert "验收标准" in top["snippet"]
    assert "验收标准\n本方案用于替换旧版报销系统" not in top["snippet"]  # 段落即粒度，不串段
    assert top["source"] == "local-docs:plan.txt"
    assert data["source"] == "local-docs:plan.txt"


async def test_file_ask_no_hit_degrades_without_fabrication(docs_dir: Path) -> None:
    """问题所涉内容不在文件内 → 零命中如实降级，不编造。"""
    (docs_dir / "plan.txt").write_text(_DOC, encoding="utf-8")
    data = await file_ask._file_ask(CTX, {"filename": "plan.txt", "query": "zzzqxj999"})
    assert data["degraded"] is True
    assert data["answer_fragments"] == []
    assert data["degraded_reason"]


async def test_file_ask_empty_file_degrades(docs_dir: Path) -> None:
    """无可检索文本（空文件/纯图扫描件）如实降级并给可操作原因。"""
    (docs_dir / "blank.txt").write_text("   \n\n", encoding="utf-8")
    data = await file_ask._file_ask(CTX, {"filename": "blank.txt", "query": "有什么内容"})
    assert data["degraded"] is True
    assert data["retrieval_mode"] == "none"
    assert "可检索文本" in data["degraded_reason"]


async def test_file_ask_rejects_bad_args(docs_dir: Path) -> None:
    """缺文件 404 / 穿越 1001 / 空问题 1001 / top_k 越界 1001。"""
    (docs_dir / "plan.txt").write_text(_DOC, encoding="utf-8")
    with pytest.raises(BusinessError) as missing:
        await file_ask._file_ask(CTX, {"filename": "gone.txt", "query": "验收标准"})
    assert missing.value.code == 1004
    with pytest.raises(BusinessError) as traversal:
        await file_ask._file_ask(CTX, {"filename": "../evil.txt", "query": "验收标准"})
    assert traversal.value.code == 1001
    with pytest.raises(BusinessError) as empty_query:
        await file_ask._file_ask(CTX, {"filename": "plan.txt", "query": "  "})
    assert empty_query.value.code == 1001
    with pytest.raises(BusinessError) as bad_top_k:
        await file_ask._file_ask(CTX, {"filename": "plan.txt", "query": "验收标准", "top_k": 99})
    assert bad_top_k.value.code == 1001


# ---------------- 向量语义检索（配 EMBEDDING_* 时） ----------------


async def test_file_ask_semantic_retrieval_when_configured(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配了 EMBEDDING_* → 走向量通道：不含「验收标准」四字也能命中该段。"""
    (docs_dir / "plan.txt").write_text(_DOC, encoding="utf-8")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "fake-embedding")

    async def fake_embed(texts: list[str]) -> list[list[float]]:
        # 只按「语义归属」给向量：问句与验收段同向（余弦 1.0），其余正交（被门槛滤掉）
        return [
            [1.0, 0.0] if ("3 个工作日" in text or "多久" in text) else [0.0, 1.0] for text in texts
        ]

    monkeypatch.setattr(retrieval, "embed_texts", fake_embed)
    data = await file_ask._file_ask(CTX, {"filename": "plan.txt", "query": "审批要多久"})
    assert data["retrieval_mode"] == "embedding"
    assert data["embedding_model"] == "fake-embedding"
    assert data["retrieval_fallback_reason"] == ""
    assert data["count"] == 1
    assert "3 个工作日" in data["answer_fragments"][0]["snippet"]


async def test_file_ask_falls_back_to_bigram_when_embedding_unreachable(
    docs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """向量端点不可达 → 降级字符检索并如实标注，绝不抛错（降级不 500 红线）。"""
    (docs_dir / "plan.txt").write_text(_DOC, encoding="utf-8")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "fake-embedding")
    monkeypatch.setattr(settings, "EMBEDDING_TIMEOUT_SECONDS", 0.5)
    data = await file_ask._file_ask(CTX, {"filename": "plan.txt", "query": "验收标准是什么"})
    assert data["retrieval_mode"] == "bigram"
    assert data["embedding_model"] == ""
    assert data["retrieval_fallback_reason"]
    assert data["count"] >= 1


# ---------------- 注册口径 ----------------


def test_file_ask_register_scope() -> None:
    """只读免审：office:read + requires_approval=False（问答不落库、无副作用）。"""
    specs = {spec.name: spec for spec in file_ask.specs()}
    assert set(specs) == {"office.file.ask"}
    assert specs["office.file.ask"].scope == "office:read"
    assert specs["office.file.ask"].requires_approval is False
