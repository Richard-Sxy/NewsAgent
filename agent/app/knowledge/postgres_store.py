"""持久化知识存储。PostgresKnowledgeStore 保存文档、切片、向量与版本，处理重复写入和更新；也提供向量检索与文档状态查询。"""

from __future__ import annotations

import math
from datetime import datetime
from hashlib import sha256
from typing import Sequence

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.db.session import Database
from app.knowledge.document import (
    IngestOutcome,
    IngestReport,
    IngestStatus,
    KnowledgeDocument,
)
from app.knowledge.native_indexing import split_text
from app.model_runtime.core import EmbeddingPort, EmbeddingRequest
from app.retrieval.tiered_vector import VectorSearchHit, VectorTier


_metadata = MetaData()
_documents = Table(
    "native_knowledge_documents",
    _metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("document_id", String(160), primary_key=True),
    Column("news_id", String(160), nullable=False),
    Column("content_version", Integer, nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("embedding_version", String(128), nullable=False),
    Column("title", String(500), nullable=False),
    Column("body", Text, nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
_chunks = Table(
    "native_knowledge_chunks",
    _metadata,
    Column("chunk_id", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("document_id", String(160), nullable=False),
    Column("news_id", String(160), nullable=False),
    Column("content_version", Integer, nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("embedding_version", String(128), nullable=False),
    Column("tier", String(16), nullable=False),
    Column("title", String(500), nullable=False),
    Column("excerpt", Text, nullable=False),
    Column("source_url", Text),
    Column("publish_time", DateTime(timezone=True), nullable=False),
    Column("vector", JSONB, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "document_id"],
        ["native_knowledge_documents.tenant_id", "native_knowledge_documents.document_id"],
    ),
)

""" 这边调用PostgreSQL去存储中间信息。 """
class PostgresKnowledgeStore:
    def __init__(
        self,
        *,
        database: Database,
        embedding: EmbeddingPort,
        embedding_version: str,
        tier: VectorTier = VectorTier.HOT,
        chunk_chars: int = 800,
        chunk_overlap: int = 80,
        embedding_batch_size: int = 64,
        max_search_rows: int = 50000,
    ) -> None:
        if not embedding_version.strip() or not 1 <= embedding_batch_size <= 2048:
            raise ValueError("knowledge embedding configuration is invalid")
        if max_search_rows <= 0:
            raise ValueError("max_search_rows must be positive")
        self._database = database
        self._embedding = embedding
        self.embedding_version = embedding_version
        self.tier = tier
        self._chunk_chars = chunk_chars
        self._overlap = chunk_overlap
        self._batch_size = embedding_batch_size
        self._max_search_rows = max_search_rows

    async def get_document_status(
        self, *, tenant_id: str, document_id: str
    ) -> dict[str, str | int] | None:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(
                        _documents.c.document_id,
                        _documents.c.news_id,
                        _documents.c.content_version,
                        _documents.c.embedding_version,
                    ).where(
                        _documents.c.tenant_id == tenant_id,
                        _documents.c.document_id == document_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def upsert_documents(
        self,
        *,
        tenant_id: str,
        documents: tuple[KnowledgeDocument, ...],
    ) -> IngestReport:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        outcomes: list[IngestOutcome] = []
        for document in documents:
            document.validate()
            try:
                news_id = document.metadata["news_id"].strip()
                content_version = int(document.metadata["content_version"])
                published = datetime.fromisoformat(document.metadata["publish_time"])
                if not news_id or content_version < 1:
                    raise ValueError("news_id and positive content_version are required")
                if published.tzinfo is None or published.utcoffset() is None:
                    raise ValueError("publish_time must include a timezone")
            except (KeyError, TypeError, ValueError) as exc:
                outcomes.append(
                    IngestOutcome(document.document_id, IngestStatus.FAILED, detail=str(exc))
                )
                continue
            content_hash = sha256(
                f"{document.title}\x1f{document.text}".encode("utf-8")
            ).hexdigest()
            parts = split_text(
                document.text,
                max_chars=self._chunk_chars,
                overlap=self._overlap,
            )
            vectors: list[tuple[float, ...]] = []
            identity = sha256(
                f"{tenant_id}\x1f{document.document_id}\x1f{content_version}".encode("utf-8")
            ).hexdigest()
            for offset in range(0, len(parts), self._batch_size):
                batch = parts[offset:offset + self._batch_size]
                result = await self._embedding.embed(
                    EmbeddingRequest(
                        tenant_id=tenant_id,
                        trace_id=f"knowledge-ingest-{identity}-{offset}",
                        model_route=self.embedding_version,
                        texts=tuple(f"{document.title}\n{part}" for part in batch),
                    )
                )
                if result.model_version != self.embedding_version or len(result.vectors) != len(batch):
                    raise ValueError("knowledge embedding version or count drifted")
                vectors.extend(result.vectors)
            lock_digest = sha256(f"{tenant_id}\x1f{document.document_id}".encode("utf-8")).digest()
            lock_key = int.from_bytes(lock_digest[:8], "big", signed=True)
            async with self._database.session() as session:
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
                existing = (
                    await session.execute(
                        select(_documents).where(
                            _documents.c.tenant_id == tenant_id,
                            _documents.c.document_id == document.document_id,
                        ).with_for_update()
                    )
                ).mappings().first()
                if existing is not None:
                    old_version = existing["content_version"]
                    same_content = existing["content_sha256"] == content_hash
                    if old_version == content_version and same_content and existing["embedding_version"] == self.embedding_version:
                        outcomes.append(
                            IngestOutcome(document.document_id, IngestStatus.SKIPPED, collection_id=f"native:{document.document_id}")
                        )
                        continue
                    if old_version > content_version or (old_version == content_version and not same_content):
                        outcomes.append(
                            IngestOutcome(document.document_id, IngestStatus.FAILED, detail="changed content requires a newer version")
                        )
                        continue
                values = {
                    "tenant_id": tenant_id,
                    "document_id": document.document_id,
                    "news_id": news_id,
                    "content_version": content_version,
                    "content_sha256": content_hash,
                    "embedding_version": self.embedding_version,
                    "title": document.title,
                    "body": document.text,
                    "metadata": dict(document.metadata),
                    "updated_at": datetime.now(published.tzinfo),
                }
                if existing is None:
                    await session.execute(insert(_documents).values(**values))
                else:
                    await session.execute(
                        update(_documents).where(
                            _documents.c.tenant_id == tenant_id,
                            _documents.c.document_id == document.document_id,
                        ).values(**values)
                    )
                    await session.execute(
                        delete(_chunks).where(
                            _chunks.c.tenant_id == tenant_id,
                            _chunks.c.document_id == document.document_id,
                        )
                    )
                rows = [
                    {
                        "chunk_id": sha256(
                            f"{tenant_id}\x1f{document.document_id}\x1f{content_version}\x1f{index}\x1f{self.embedding_version}".encode("utf-8")
                        ).hexdigest(),
                        "tenant_id": tenant_id,
                        "document_id": document.document_id,
                        "news_id": news_id,
                        "content_version": content_version,
                        "chunk_index": index,
                        "embedding_version": self.embedding_version,
                        "tier": self.tier.value,
                        "title": document.title,
                        "excerpt": part[:1000],
                        "source_url": document.source_url or None,
                        "publish_time": published,
                        "vector": list(vector),
                    }
                    for index, (part, vector) in enumerate(zip(parts, vectors, strict=True))
                ]
                await session.execute(insert(_chunks), rows)
                outcomes.append(
                    IngestOutcome(
                        document.document_id,
                        IngestStatus.CREATED if existing is None else IngestStatus.UPDATED,
                        collection_id=f"native:{document.document_id}",
                    )
                )
        return IngestReport(tuple(outcomes))

    """这边是在PostgreSQL当中查询新闻标题。"""
    async def search(
        self,
        *,
        tier: VectorTier,
        tenant_id: str,
        vector: Sequence[float],
        embedding_version: str,
        exclude_news_ids: frozenset[str],
        limit: int,
    ) -> list[VectorSearchHit]:
        if not tenant_id.strip() or not vector or limit <= 0:
            raise ValueError("knowledge search context is invalid")
        statement = select(_chunks).where(
            _chunks.c.tenant_id == tenant_id,
            _chunks.c.tier == tier.value,
            _chunks.c.embedding_version == embedding_version,
        )
        if exclude_news_ids:
            statement = statement.where(_chunks.c.news_id.not_in(exclude_news_ids))
        async with self._database.session() as session:
            rows = (
                await session.execute(statement.limit(self._max_search_rows + 1))
            ).mappings().all()
        if len(rows) > self._max_search_rows:
            raise RuntimeError("knowledge search scan limit exceeded; use a vector index")
        hits: list[VectorSearchHit] = []
        for row in rows:
            coordinates = row["vector"]
            if len(coordinates) != len(vector):
                raise ValueError("knowledge query and index dimensions differ")
            score = sum(float(left) * float(right) for left, right in zip(coordinates, vector, strict=True))
            if not math.isfinite(score):
                raise ValueError("knowledge vector score is non-finite")
            hits.append(
                VectorSearchHit(
                    chunk_id=row["chunk_id"],
                    news_id=row["news_id"],
                    title=row["title"],
                    excerpt=row["excerpt"],
                    source_url=row["source_url"],
                    publish_time=row["publish_time"],
                    raw_score=score,
                    tier=tier,
                    content_version=row["content_version"],
                    embedding_version=row["embedding_version"],
                )
            )
        hits.sort(key=lambda item: (-item.raw_score, item.chunk_id))
        return hits[:limit]
