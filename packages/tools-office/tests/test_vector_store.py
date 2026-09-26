"""Milvus 向量库与三级降级链单测（ADR-0005 阶段二，全程零网络）。

口径：FakeVectorStore 实现 VectorStore Protocol（内存版），覆盖增量差集 / 门槛过滤 /
origin 过滤 / 降级三分支 / mode 标签；真实 Milvus 的集成用例标 @pytest.mark.milvus，
MILVUS_URI 未配置即 skip（CI 与本地默认不依赖 Docker）。
"""

from __future__ import annotations

import pytest

from office_agent_core import kv
from office_agent_core.settings import settings
from office_agent_tools_office import retrieval, vector_store
from office_agent_tools_office.retrieval import retrieve, retrieve_by_store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class FakeVectorStore:
    """内存版 VectorStore：记录 upsert/search 调用，用于断言增量与过滤口径。"""

    def __init__(self) -> None:
        self.rows: dict[str, vector_store.VectorRecord] = {}
        self.upsert_calls: list[list[str]] = []
        self.search_origins: list[list[str] | None] = []
        self.fail_search = False
        #: 简易「向量」= 文本长度归一值，保证同文本同分数、确定性可断言
        self._vectors: dict[str, list[float]] = {}

    def available(self) -> bool:
        return True

    async def known_ids(self, ids: list[str]) -> set[str]:
        return {value for value in ids if value in self.rows}

    async def upsert(self, records: list[vector_store.VectorRecord]) -> int:
        self.upsert_calls.append([record.id for record in records])
        for record in records:
            self.rows[record.id] = record
            self._vectors[record.id] = record.vector
        return len(records)

    async def search(self, query_vector, top_k, min_score, origins=None):
        self.search_origins.append(origins)
        if self.fail_search:
            raise ConnectionError("milvus down")
        scored = []
        for record in self.rows.values():
            if origins and record.origin not in origins:
                continue
            score = retrieval.cosine(query_vector, self._vectors[record.id])
            if score >= min_score:
                scored.append((score, record))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:top_k]

    async def delete_by_source(self, origin: str, source: str) -> int:
        victims = [
            key
            for key, record in self.rows.items()
            if record.origin == origin and record.source == source
        ]
        for key in victims:
            self.rows.pop(key, None)
        return len(victims)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """默认钉空外部配置：Milvus 关闭、embedding 关闭，确保用例零网络。"""
    monkeypatch.setattr(settings, "MILVUS_URI", "")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "")
    monkeypatch.setattr(settings, "REDIS_URL", "")
    vector_store.reset_store()
    kv.reset()
    yield
    vector_store.reset_store()
    kv.reset()


def _chunks() -> list[dict[str, str]]:
    return [
        {"title": "报销制度", "text": "报销需在 30 天内提交", "source": "builtin-demo"},
        {"title": "考勤制度", "text": "工作日 9:00-18:00", "source": "builtin-demo"},
    ]


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    """确定性假向量：base_url 空也要能被 patch 顶替（长度归一，同文本同向量）。"""
    return [[len(text) / 100.0, 1.0] for text in texts]


# ---------------- ① record_id：文本/归属变更即新 id ----------------


def test_record_id_changes_with_text_and_origin():
    base = vector_store.record_id("knowledge", "a.md", "正文")
    assert base == vector_store.record_id("knowledge", "a.md", "正文")  # 同输入稳定
    assert base != vector_store.record_id("knowledge", "a.md", "正文改")  # 文本变更
    assert base != vector_store.record_id("docs", "a.md", "正文")  # 归属变更
    assert len(base) == 40  # sha1 hex


# ---------------- ② get_store：URI 空即 None（零网络零构造） ----------------


def test_get_store_none_without_uri():
    assert vector_store.get_store() is None


def test_get_store_builds_lazily_without_connecting(monkeypatch):
    """配了 URI 即拿到实例（构造期零网络）——建连延到首个真实调用。

    MilvusClient() 会急切 gRPC 握手、对端离线抛 MilvusException（实测非 ImportError）；
    若在构造期抛，retrieve 连「记原因后降级」的机会都没有（见 retrieval.retrieve 注释）。
    """
    monkeypatch.setattr(settings, "MILVUS_URI", "http://127.0.0.1:1")  # 不调用即不建连
    store = vector_store.get_store()
    assert isinstance(store, vector_store.MilvusVectorStore)
    assert store.available() is True
    assert store._client is None  # 惰性：构造后仍未建连


# ---------------- ③ 增量灌库：只 embed 缺失块 ----------------


async def test_retrieve_by_store_ingests_only_missing(monkeypatch):
    store = FakeVectorStore()
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)
    chunks = _chunks()

    first = await retrieve_by_store("报销", chunks, 3, store)
    assert len(store.upsert_calls) == 1 and len(store.upsert_calls[0]) == 2  # 首查灌两块
    assert first and first[0][1]["title"] == "报销制度"

    second = await retrieve_by_store("报销", chunks, 3, store)
    assert len(store.upsert_calls) == 1  # 第二次无新增：差集为空不 embed 不 upsert
    assert second[0][1]["title"] == "报销制度"

    changed = [*chunks, {"title": "新条目", "text": "新增内容", "source": "x.md"}]
    await retrieve_by_store("报销", changed, 3, store)
    assert len(store.upsert_calls) == 2 and len(store.upsert_calls[1]) == 1  # 只补新块


# ---------------- ④ origin 过滤：五源隔离 ----------------


async def test_retrieve_by_store_filters_by_origin(monkeypatch):
    store = FakeVectorStore()
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)
    chunks = [
        {"title": "知识", "text": "制度正文", "source": "kb.md", "origin": "knowledge"},
        {"title": "待办", "text": "制度相关待办", "source": "affairs", "origin": "affairs"},
    ]
    await retrieve_by_store("制度", chunks, 5, store)
    assert store.search_origins[-1] == ["affairs", "knowledge"]  # 两源合查（本批块集覆盖）


async def test_retrieve_by_store_labels_default_origin(monkeypatch):
    """块未自带 origin 时用调用方声明的域打标（file.ask → docs / image.ask → image）。"""
    store = FakeVectorStore()
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)
    await retrieve_by_store("报销", _chunks(), 3, store, "docs")
    assert {record.origin for record in store.rows.values()} == {"docs"}
    assert store.search_origins[-1] == ["docs"]


# ---------------- ⑤ 门槛：低于 EMBEDDING_MIN_SCORE 不算命中 ----------------


async def test_retrieve_by_store_applies_min_score(monkeypatch):
    store = FakeVectorStore()
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)
    monkeypatch.setattr(settings, "EMBEDDING_MIN_SCORE", 0.999)
    hits = await retrieve_by_store("报销", _chunks(), 3, store)
    assert hits == []  # 门槛极高 → 无命中（门槛是取舍杆，此处验证过滤确实生效）


# ---------------- ⑥ 降级链三分支 ----------------


async def test_retrieve_falls_back_to_bigram_when_store_fails(monkeypatch):
    """store 抛错且 embedding 也不可用 → 直落字符通道，降级原因链如实标注。"""
    store = FakeVectorStore()
    store.fail_search = True
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "fake")

    async def _boom(texts):
        raise ConnectionError("embedding down")

    monkeypatch.setattr(retrieval, "embed_texts", _boom)
    monkeypatch.setattr(vector_store, "get_store", lambda: store)

    scored, mode, reason = await retrieve("报销", _chunks(), 3, origin="knowledge")
    assert mode == "bigram"
    assert "Milvus 不可用" in reason and "ConnectionError" in reason
    assert scored  # 字符通道命中（报销二字）


async def test_retrieve_uses_store_when_available(monkeypatch):
    store = FakeVectorStore()
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)
    monkeypatch.setattr(vector_store, "get_store", lambda: store)
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "fake")

    scored, mode, reason = await retrieve("报销制度", _chunks(), 3, origin="knowledge")
    assert mode == "milvus" and reason == ""
    assert scored and scored[0][1]["title"] == "报销制度"


async def test_retrieve_falls_back_to_embedding_bypassing_store(monkeypatch):
    """store 取到但 embedding 全量重算可用、store 抛错 → 中间级承接（milvus→embedding）。"""
    store = FakeVectorStore()
    store.fail_search = True
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)
    monkeypatch.setattr(vector_store, "get_store", lambda: store)
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "fake")

    scored, mode, reason = await retrieve("报销", _chunks(), 3, origin="knowledge")
    assert mode == "embedding"
    assert "Milvus 不可用" in reason
    assert scored


async def test_retrieve_survives_store_factory_error(monkeypatch):
    """取 store 本身抛错（依赖/配置意外）也不得穿透：记原因后照常降下一级。"""
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "fake")

    def _boom_store():
        raise RuntimeError("pymilvus 初始化炸了")

    monkeypatch.setattr(vector_store, "get_store", _boom_store)
    monkeypatch.setattr(retrieval, "embed_texts", _fake_embed)

    scored, mode, reason = await retrieve("报销", _chunks(), 3, origin="knowledge")
    assert mode == "embedding"
    assert "Milvus 不可用" in reason and "RuntimeError" in reason
    assert scored


async def test_retrieve_zero_network_when_unconfigured(monkeypatch):
    """全未配置：既不构造 store 也不发 embedding 网络，直接字符通道（现行为不变）。"""
    called = {"n": 0}

    async def _boom(texts):
        called["n"] += 1
        raise AssertionError("不应发起 embedding 网络调用")

    monkeypatch.setattr(retrieval, "embed_texts", _boom)
    scored, mode, reason = await retrieve("报销", _chunks(), 3, origin="knowledge")
    assert called["n"] == 0
    assert mode == "bigram" and reason == "" and scored


# ---------------- ⑦ 集成用例（需真实 Milvus，未配置即 skip） ----------------


@pytest.mark.milvus
async def test_milvus_integration_roundtrip():
    if not settings.MILVUS_URI.strip():
        pytest.skip("MILVUS_URI 未配置：跳过真实 Milvus 集成用例（见 deploy/docker-compose.yml）")
    store = vector_store.get_store()
    assert store is not None
    record = vector_store.VectorRecord(
        id=vector_store.record_id("it", "it.md", "集成测试正文"),
        origin="it",
        source="it.md",
        title="集成",
        text="集成测试正文",
        text_hash="x",
        vector=[1.0, 0.0],
    )
    assert await store.upsert([record]) == 1
    assert record.id in await store.known_ids([record.id])
    hits = await store.search([1.0, 0.0], 3, 0.0, origins=["it"])
    assert hits and hits[0][1].source == "it.md"
    assert await store.delete_by_source("it", "it.md") == 1
