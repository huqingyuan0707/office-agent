"""kb.ask 单元测试（handler 直调，不起 HTTP 服务）。

覆盖：字符 bigram 检索与降级留白、本地知识文件装载、向量语义检索通道与
      端点不可达时的回退标注（降级绝不 500）。
对齐：AGENTS.md §3（降级绝不 500 / 数据不出域）、§5（验证命令）；
      智能办公Agent 产品需求文档.md §5.1（V1.0 知识库问答）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_agent_core.contracts import ToolContext
from office_agent_core.errors import BusinessError
from office_agent_core.settings import settings
from office_agent_tools_office import kb, retrieval

CTX = ToolContext(tenant="t1", username="alice", roles=["office:read", "office:write"])


@pytest.fixture(autouse=True)
def _kb_bigram_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """单测默认钉住「未配向量检索」：不给 EMBEDDING_* 就零网络走字符检索。

    否则本机 `.env` 一旦登记 EMBEDDING_*，整个用例集都会去打真实 /embeddings。
    要测向量通道的用例在自身内再 monkeypatch 覆盖。
    Milvus 同理钉空（MILVUS_URI 一登记，检索会先试向量库，同样会打网络）。
    """
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "")
    monkeypatch.setattr(settings, "MILVUS_URI", "")


# ---------------- 字符检索（默认通道） ----------------


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
    assert data["retrieval_mode"] == "bigram"


# ---------------- 向量语义检索（配 EMBEDDING_* 时） ----------------


async def test_kb_ask_semantic_retrieval_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配了 EMBEDDING_* → 走向量通道：改说法（不含「差旅」二字）也能命中。"""
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "fake-embedding")

    async def fake_embed(texts: list[str]) -> list[list[float]]:
        # 只按「语义归属」给向量：问句与差旅段落同向（余弦 1.0），其余正交（0.0 被门槛滤掉）
        return [[1.0, 0.0] if ("签字" in text or "差旅" in text) else [0.0, 1.0] for text in texts]

    monkeypatch.setattr(retrieval, "embed_texts", fake_embed)
    data = await kb._kb_ask(CTX, {"query": "出差花销找谁签字"})
    assert data["retrieval_mode"] == "embedding"
    assert data["embedding_model"] == "fake-embedding"
    assert data["retrieval_fallback_reason"] == ""
    assert data["count"] == 1
    assert data["results"][0]["title"] == "差旅标准（演示条目）"
    assert data["results"][0]["score"] == 1.0
    # 向量通道的 snippet = 命中段落整段（不再按字符窗口截）
    assert "住宿一线城市" in data["results"][0]["snippet"]


async def test_kb_ask_falls_back_to_bigram_when_embedding_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """向量端点不可达 → 降级字符检索并如实标注，绝不抛错（降级不 500 红线）。"""
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "fake-embedding")
    monkeypatch.setattr(settings, "EMBEDDING_TIMEOUT_SECONDS", 0.5)
    data = await kb._kb_ask(CTX, {"query": "考勤迟到怎么算"})
    assert data["retrieval_mode"] == "bigram"
    assert data["embedding_model"] == ""
    assert data["retrieval_fallback_reason"]
    assert data["count"] >= 1
