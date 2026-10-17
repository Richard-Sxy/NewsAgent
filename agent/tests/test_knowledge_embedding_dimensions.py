"""Dimension drift must never replace a ready document or poison its index.

The PostgreSQL store executes its real SQLAlchemy statements against isolated
SQLite tables/transactions here. Only PostgreSQL advisory locking and its JSONB
length function use local equivalents; no deployed database is touched.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import JSON, MetaData, create_engine, event, select
from sqlalchemy.orm import Session

from app.modules.knowledge.document import IngestStatus, KnowledgeDocument
from app.modules.knowledge.native_indexing import NativeKnowledgeIndexingService
from app.modules.knowledge.postgres_store import PostgresKnowledgeStore, _chunks, _documents, _metadata
from app.model_runtime.core import EmbeddingResult
from app.retrieval.in_memory_index import InMemoryTieredVectorIndex
from app.retrieval.tiered_vector import VectorTier


class ScriptedEmbedding:
    def __init__(self):
        self.set_dimensions(2)

    def set_dimensions(self, *dimensions):
        self.dimensions, self.calls = dimensions, 0

    async def embed(self, request):
        dimension = self.dimensions[min(self.calls, len(self.dimensions) - 1)]
        self.calls += 1
        return EmbeddingResult(
            vectors=tuple((1.0,) * dimension for _ in request.texts),
            model_version=request.model_route,
        )


class TransactionalDatabase:
    def __init__(self):
        self.engine = create_engine("sqlite://")

        @event.listens_for(self.engine, "connect")
        def functions(connection, _):
            connection.create_function("pg_advisory_xact_lock", 1, lambda _: 0)
            connection.create_function("jsonb_array_length", 1, lambda value: len(json.loads(value)))

        schema = MetaData()
        for table in _metadata.tables.values():
            copied = table.to_metadata(schema)
            for column in copied.columns:
                if column.name in {"metadata", "vector"}:
                    column.type = JSON()
        schema.create_all(self.engine)

    @asynccontextmanager
    async def session(self):
        with Session(self.engine) as current:
            async def execute(*args, **kwargs):
                return current.execute(*args, **kwargs)
            try:
                yield SimpleNamespace(execute=execute)
                current.commit()
            except Exception:
                current.rollback()
                raise

    def snapshot(self):
        with self.engine.connect() as connection:
            return tuple(
                tuple(dict(row) for row in connection.execute(
                    select(table).order_by(*table.primary_key.columns)
                ).mappings())
                for table in (_documents, _chunks)
            )


@pytest.fixture(params=("native", "postgres"))
def runtime(request):
    embedding = ScriptedEmbedding()
    if request.param == "native":
        index = InMemoryTieredVectorIndex()
        store = NativeKnowledgeIndexingService(
            embedding=embedding, index=index, embedding_version="embedding-v1",
            max_chunk_chars=3, chunk_overlap=0, embedding_batch_size=1,
        )

        def snapshot():
            return (dict(store._ledger), {tier: dict(rows) for tier, rows in index._rows.items()})

        yield SimpleNamespace(store=store, index=index, embedding=embedding,
                              snapshot=snapshot, reopen=lambda: store)
    else:
        database = TransactionalDatabase()

        def build_store():
            return PostgresKnowledgeStore(
                database=database, embedding=embedding, embedding_version="embedding-v1",
                chunk_chars=3, chunk_overlap=0, embedding_batch_size=1,
            )

        store = build_store()
        yield SimpleNamespace(store=store, index=store, embedding=embedding,
                              snapshot=database.snapshot, reopen=build_store)
        database.engine.dispose()


def document(*, news_id="one", version=1, text="abcdef"):
    return KnowledgeDocument(
        document_id=f"news-{news_id}", title="测试新闻", text=text,
        media_type="article", text_source="test",
        metadata={"news_id": news_id, "content_version": str(version),
                  "publish_time": datetime(2026, 10, 12, tzinfo=UTC).isoformat()},
    )


async def assert_old_document_is_searchable(runtime):
    hits = await runtime.index.search(
        tier=VectorTier.HOT, tenant_id="tenant-one", vector=(1.0, 1.0),
        embedding_version="embedding-v1", exclude_news_ids=frozenset(), limit=10,
    )
    assert hits and {hit.news_id for hit in hits} == {"one"}
    assert {hit.content_version for hit in hits} == {1}
    assert [hit.excerpt for hit in hits] == ["abc", "def"] or [hit.excerpt for hit in hits] == ["def", "abc"]


@pytest.mark.asyncio
async def test_cross_batch_dimension_drift_preserves_ready_document_and_chunks(runtime):
    result = await runtime.store.upsert_documents(tenant_id="tenant-one", documents=(document(),))
    assert result.outcomes[0].status is IngestStatus.CREATED
    before = runtime.snapshot()
    runtime.embedding.set_dimensions(2, 3)
    with pytest.raises(ValueError, match="dimensions"):
        await runtime.store.upsert_documents(
            tenant_id="tenant-one", documents=(document(version=2, text="ghijkl"),),
        )
    assert runtime.snapshot() == before
    await assert_old_document_is_searchable(runtime)


@pytest.mark.asyncio
async def test_same_version_new_document_dimension_drift_preserves_index_after_reopen(runtime):
    await runtime.store.upsert_documents(tenant_id="tenant-one", documents=(document(),))
    before = runtime.snapshot()
    runtime.embedding.set_dimensions(3)
    with pytest.raises(ValueError, match="dimensions"):
        await runtime.reopen().upsert_documents(
            tenant_id="tenant-one", documents=(document(news_id="two"),),
        )
    assert runtime.snapshot() == before
    await assert_old_document_is_searchable(runtime)


@pytest.mark.asyncio
async def test_dimension_scope_does_not_mix_tenants(runtime):
    await runtime.store.upsert_documents(tenant_id="tenant-one", documents=(document(),))
    runtime.embedding.set_dimensions(3)
    result = await runtime.store.upsert_documents(
        tenant_id="tenant-two", documents=(document(news_id="two"),),
    )
    assert result.outcomes[0].status is IngestStatus.CREATED
    await assert_old_document_is_searchable(runtime)
