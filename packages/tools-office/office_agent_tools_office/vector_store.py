"""Milvus 向量库抽象与实现（ADR-0005 阶段二：把「每次全量重算」降为「只算增量」）。

职责：
- VectorRecord / VectorStore Protocol：检索侧与存储侧的唯一契约面（FakeVectorStore 在
  单测里实现同一 Protocol，零网络覆盖增量差集/门槛/降级分支）；
- MilvusVectorStore：pymilvus 同步 SDK 经 asyncio.to_thread 包装，异常一律上抛给
  调用方（retrieval.retrieve）做三级降级裁决——本模块只抛错不吞错、绝不 500；
- get_store()：MILVUS_URI 为空即返回 None（整体关闭、零网络，现行为不变）；
  pymilvus 缺失只告警一次并永久缺席（可选依赖，仿 pptx_gen 的依赖缺失不注册口径）。

设计口径（对齐 ADR-0005 / 迁移方案 §阶段二）：
- **单 collection + origin 标量字段**：五源联查一次带 filter 合查；语料量级小；
  visibility 权限**不进向量库**——回表过滤口径不动（kb.py 检索前过滤），杜绝权限第二真相源；
- 度量 COSINE + 门槛复用 EMBEDDING_MIN_SCORE（与现余弦全量重算同构，分数口径不漂移）；
- 块 id = sha1(origin|source|text)：文本变更即新 id，known_ids 差集天然覆盖增量；
- 向量维度不硬编码：collection 首次灌库时按首条真实向量维度建（换模型走 reindex）。

链路：retrieval.retrieve_by_store → 本模块 →（pymilvus → Milvus standalone，Docker Compose）。
红线：不读业务库、不触 ORM / FastAPI；凭据只走环境变量（MILVUS_TOKEN）。
对齐：AGENTS.md §3（降级绝不 500 / 零硬编码）；docs/ADR-0005 §4。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from office_agent_core.settings import settings

logger = logging.getLogger(__name__)

#: 单次 known_ids 反查的 id 批量上限（Milvus in 表达式过长会拒）
_ID_BATCH = 96


def record_id(origin: str, source: str, text: str) -> str:
    """块唯一 id：sha1(origin|source|text)——文本或归属一变即新 id（增量差集的前提）。"""
    digest = hashlib.sha1(f"{origin}|{source}|{text}".encode(), usedforsecurity=False)
    return digest.hexdigest()


@dataclass
class VectorRecord:
    """一个待存/已存的段落块向量（vector 在灌库前由调用方 embed 后填入）。"""

    id: str
    origin: str
    source: str
    title: str
    text: str
    text_hash: str
    vector: list[float] = field(default_factory=list)


class VectorStore(Protocol):
    """向量库窄接口（全异步；实现侧任何异常上抛，由 retrieval 降级裁决）。"""

    def available(self) -> bool:
        """配置面是否可用（不代表连接已建立——连接失败在首个调用处抛错）。"""
        ...

    async def known_ids(self, ids: list[str]) -> set[str]:
        """返回已入库的 id 子集（差集即本次需 embed 的增量块）。"""
        ...

    async def upsert(self, records: list[VectorRecord]) -> int:
        """写入/覆盖向量记录（collection 不存在时按首条向量维度惰性创建）。"""
        ...

    async def search(
        self,
        query_vector: list[float],
        top_k: int,
        min_score: float,
        origins: list[str] | None = None,
    ) -> list[tuple[float, VectorRecord]]:
        """余弦检索（origins 非空时按标量字段过滤），返回 (分数, 记录)，低于门槛的不算命中。"""
        ...

    async def delete_by_source(self, origin: str, source: str) -> int:
        """删除某来源的全部块（文件删除/整篇重灌用），返回删除数。"""
        ...


def _escape(value: str) -> str:
    """Milvus 表达式字符串字面量转义（反斜杠与双引号）。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class MilvusVectorStore:
    """pymilvus MilvusClient 的异步包装（同步调用全部 to_thread，不阻塞事件循环）。"""

    def __init__(self) -> None:
        # 构造期**不建连**：MilvusClient() 会急切 gRPC 握手，对端离线即抛 MilvusException
        # （非 ImportError），若在此抛出就连「记原因后降级」的机会都没有；延到首个真实
        # 调用（都在 to_thread 里）再建，失败由 retrieval.retrieve 捕获并如实标注。
        self._collection = settings.MILVUS_COLLECTION.strip() or "office_chunks"
        self._timeout = float(settings.MILVUS_TIMEOUT_SECONDS)
        self._client: Any | None = None
        self._schema_ready: asyncio.Future | None = None

    def _cli(self) -> Any:
        """惰性建连（仅可在同步调用内使用——构造有网络 I/O，故调用方都走 to_thread）。"""
        if self._client is None:
            from pymilvus import MilvusClient  # 可选依赖：缺失即抛 ImportError 给上层降级

            self._client = MilvusVectorStore._new_client(MilvusClient)
        return self._client

    @staticmethod
    def _new_client(client_cls: type) -> Any:
        token = settings.MILVUS_TOKEN.get_secret_value()
        return client_cls(
            uri=settings.MILVUS_URI.strip(),
            token=token or None,
            timeout=float(settings.MILVUS_TIMEOUT_SECONDS),
        )

    def available(self) -> bool:
        return bool(settings.MILVUS_URI.strip())

    # ---- collection 惰性建（维度取首条真实向量，不硬编码）----

    def _ensure_collection_sync(self, dim: int) -> None:
        from pymilvus import DataType

        if self._cli().has_collection(self._collection, timeout=self._timeout):
            return
        schema = self._cli().create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("origin", DataType.VARCHAR, max_length=64)
        schema.add_field("source", DataType.VARCHAR, max_length=512)
        schema.add_field("title", DataType.VARCHAR, max_length=512)
        schema.add_field("text", DataType.VARCHAR, max_length=8192)
        schema.add_field("text_hash", DataType.VARCHAR, max_length=64)
        index_params = self._cli().prepare_index_params()
        index_params.add_index(field_name="vector", index_type="FLAT", metric_type="COSINE")
        self._cli().create_collection(
            self._collection, schema=schema, index_params=index_params, timeout=self._timeout
        )

    async def _ensure_collection(self, dim: int) -> None:
        await asyncio.to_thread(self._ensure_collection_sync, dim)

    # ---- Protocol 实现 ----

    async def known_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        found = set[str]()
        for start in range(0, len(ids), _ID_BATCH):
            batch = ids[start : start + _ID_BATCH]
            rows = await asyncio.to_thread(self._query_ids_sync, batch)
            found.update(rows)
        return found

    def _query_ids_sync(self, batch: list[str]) -> list[str]:
        if not self._cli().has_collection(self._collection, timeout=self._timeout):
            return []
        expr = "id in [" + ",".join(f'"{value}"' for value in batch) + "]"
        rows = self._cli().query(
            self._collection, filter=expr, output_fields=["id"], timeout=self._timeout
        )
        return [str(row.get("id")) for row in rows]

    async def upsert(self, records: list[VectorRecord]) -> int:
        if not records:
            return 0
        await self._ensure_collection(len(records[0].vector))
        rows = [
            {
                "id": record.id,
                "vector": record.vector,
                "origin": record.origin,
                "source": record.source,
                "title": record.title,
                "text": record.text,
                "text_hash": record.text_hash,
            }
            for record in records
        ]
        await asyncio.to_thread(self._cli().upsert, self._collection, rows, timeout=self._timeout)
        return len(rows)

    async def search(
        self,
        query_vector: list[float],
        top_k: int,
        min_score: float,
        origins: list[str] | None = None,
    ) -> list[tuple[float, VectorRecord]]:
        hits = await asyncio.to_thread(self._search_sync, query_vector, top_k, origins or [])
        return [(float(distance), record) for distance, record in hits if distance >= min_score]

    def _search_sync(self, query_vector: list[float], top_k: int, origins: list[str]) -> list:
        if not self._cli().has_collection(self._collection, timeout=self._timeout):
            return []
        expr = ""
        if origins:
            joined = ",".join(f'"{_escape(origin)}"' for origin in origins)
            expr = f"origin in [{joined}]"
        results = self._cli().search(
            self._collection,
            data=[query_vector],
            limit=top_k,
            filter=expr,
            output_fields=["id", "origin", "source", "title", "text", "text_hash"],
            search_params={"metric_type": "COSINE"},
            timeout=self._timeout,
        )
        hits = results[0] if results else []
        pairs = []
        for hit in hits:
            entity = hit.get("entity") or hit
            pairs.append(
                (
                    float(hit.get("distance", 0.0)),
                    VectorRecord(
                        id=str(entity.get("id", "")),
                        origin=str(entity.get("origin", "")),
                        source=str(entity.get("source", "")),
                        title=str(entity.get("title", "")),
                        text=str(entity.get("text", "")),
                        text_hash=str(entity.get("text_hash", "")),
                    ),
                )
            )
        return pairs

    async def delete_by_source(self, origin: str, source: str) -> int:
        return await asyncio.to_thread(self._delete_by_source_sync, origin, source)

    def _delete_by_source_sync(self, origin: str, source: str) -> int:
        if not self._cli().has_collection(self._collection, timeout=self._timeout):
            return 0
        expr = f'origin == "{_escape(origin)}" and source == "{_escape(source)}"'
        rows = self._cli().query(
            self._collection, filter=expr, output_fields=["id"], timeout=self._timeout
        )
        ids = [str(row.get("id")) for row in rows]
        if ids:
            self._cli().delete(self._collection, ids=ids, timeout=self._timeout)
        return len(ids)


_store: VectorStore | None = None
_store_built = False


def get_store() -> VectorStore | None:
    """按配置取进程级单例：URI 未配置或 pymilvus 缺失即 None（调用方走降级链）。

    此处**不建连**（MilvusVectorStore 构造也只读配置）——对端离线/超时在首个真实调用
    抛出，由 retrieval.retrieve 记录原因后降下一级，降级链的如实标注不丢。
    """
    global _store, _store_built
    if not settings.MILVUS_URI.strip():
        return None
    if _store_built:
        return _store
    _store_built = True
    try:
        import pymilvus  # noqa: F401
    except ImportError:
        logger.warning("pymilvus 未安装：Milvus 向量库缺席，检索走全量重算降级链（ADR-0005）")
        _store = None
    else:
        _store = MilvusVectorStore()
    return _store


def reset_store() -> None:
    """测试钩子：清掉进程级单例（配置在 monkeypatch 后需重建）。"""
    global _store, _store_built
    _store = None
    _store_built = False
