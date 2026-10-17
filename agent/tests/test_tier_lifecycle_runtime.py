"""Persistent lifecycle replay through real service, SQL ledger and vector Port.

SQLite transactions make these tests isolated and deterministic. They verify
the state machine and restart boundaries, not PostgreSQL lock contention,
Temporal delivery or enterprise ANN latency/quality.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.knowledge.document import KnowledgeDocument
from app.knowledge.tier_lifecycle import KnowledgeLifecycleService
from app.knowledge.tier_lifecycle_config import (
    KnowledgeLifecycleConfig, load_knowledge_lifecycle_config,
)
from app.knowledge.tier_lifecycle_index import GuardedTieredIndex
from app.knowledge.tier_lifecycle_local_index import LocalLifecycleVectorIndex
from app.knowledge.tier_lifecycle_query import LifecycleKnowledgeSearch, LifecycleQueryUnavailable
from app.knowledge.tier_lifecycle_store import TierLifecycleStore, _metadata
from app.model_runtime.core import EmbeddingResult
from app.retrieval.tiered_vector import VectorTier


NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
TENANT = "tenant-lifecycle"


class Database:
    def __init__(self, path):
        self.engine = create_engine(f"sqlite:///{path}")
        _metadata.create_all(self.engine)

    @asynccontextmanager
    async def session(self):
        with Session(self.engine) as current:
            async def execute(*args, **kwargs):
                return current.execute(*args, **kwargs)

            try:
                yield SimpleNamespace(execute=execute)
                current.commit()
            except BaseException:
                current.rollback()
                raise


class Embedding:
    def __init__(self, version):
        self.calls = 0
        self.version = version

    async def embed(self, request):
        self.calls += 1
        return EmbeddingResult(
            model_version=request.model_route,
            vectors=tuple((1.0, 0.0) for _ in request.texts),
        )

    async def embed_queries(self, texts, *, tenant_id):
        return [(1.0, 0.0) for _ in texts]


@pytest.fixture
def runtime(tmp_path):
    config_path = Path(__file__).resolve().parents[1] / "deploy/knowledge-lifecycle.local.example.yml"
    values = load_knowledge_lifecycle_config(config_path).model_dump()
    config = KnowledgeLifecycleConfig.model_validate(values | {
        "expected_embedding_dimensions": 2, "chunk_chars": 8, "chunk_overlap": 1,
        "embedding_batch_size": 2, "vector_batch_size": 2,
        "query_candidate_limit": 3, "query_max_candidates": 24,
    })
    database = Database(tmp_path / "lifecycle.sqlite")
    ledger = TierLifecycleStore(database=database)
    raw_index = LocalLifecycleVectorIndex(
        database=database, max_search_rows=1000, environment="e2e",
    )
    guarded = GuardedTieredIndex(raw_index, ledger, max_candidates=config.query_max_candidates)
    embedding = Embedding(config.embedding_version)

    def service(index=None):
        return KnowledgeLifecycleService(
            ledger=TierLifecycleStore(database=database), index=index or raw_index,
            embedding=embedding, config=config,
        )

    query = LifecycleKnowledgeSearch(index=guarded, ledger=ledger, embedder=embedding, config=config)
    yield SimpleNamespace(
        database=database, ledger=ledger, raw_index=raw_index, guarded=guarded,
        embedding=embedding, service=service(), reopen=service, config=config, query=query,
    )
    database.engine.dispose()


def document(news_id="story", *, version=1, age_seconds=1, text="新闻正文甲乙丙丁戊己庚辛"):
    return KnowledgeDocument(
        document_id=f"document-{news_id}", title=f"标题-{version}", text=text,
        media_type="article", text_source="fixture", source_url="https://example.test/news",
        metadata={"news_id": news_id, "content_version": str(version),
                  "publish_time": (NOW - timedelta(seconds=age_seconds)).isoformat()},
    )


async def index_document(runtime, doc=None, *, tenant_id=TENANT):
    job = await runtime.service.accept(tenant_id=tenant_id, document=doc or document(), now=NOW)
    await runtime.service.process_job(tenant_id=tenant_id, job_id=job["job_id"])
    return await runtime.ledger.get_job(tenant_id=tenant_id, job_id=job["job_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("bounds", [
    {"max_chunks_per_document": 2},
    {"max_vector_coordinates_per_document": 4},
])
async def test_oversize_projection_fails_before_chunk_allocation_and_embedding(runtime, bounds, monkeypatch):
    runtime.service.config = KnowledgeLifecycleConfig.model_validate(
        runtime.config.model_dump() | bounds)
    job = await runtime.service.accept(tenant_id=TENANT,
        document=document(text="超长正文" * 20), now=NOW)
    def unexpected_split(*args, **kwargs):
        pytest.fail("over-budget document must be rejected before allocating chunks")
    monkeypatch.setattr("app.knowledge.tier_lifecycle.split_text", unexpected_split)
    with pytest.raises(ValueError, match="memory budget"):
        await runtime.service.process_job(tenant_id=TENANT, job_id=job["job_id"])
    assert runtime.embedding.calls == 0
    saved = await runtime.ledger.get_job(tenant_id=TENANT, job_id=job["job_id"])
    assert saved["status"] == "failed" and not saved["records"]


@pytest.mark.asyncio
@pytest.mark.parametrize("vector", [(1.0,), (float("nan"), 0.0), (float("inf"), 0.0)])
async def test_query_embedding_drift_rejected_even_when_index_is_empty(runtime, vector):
    class Drift:
        version = runtime.config.embedding_version
        async def embed_queries(self, texts, *, tenant_id):
            return [vector]
    runtime.query.embedder = Drift()
    with pytest.raises(LifecycleQueryUnavailable, match="unavailable"):
        await runtime.query.search(query_text="新闻", tenant_id=TENANT)


async def hits(runtime, *, tier=VectorTier.HOT, index=None, tenant_id=TENANT):
    return await (index or runtime.guarded).search(
        tier=tier, tenant_id=tenant_id, vector=(1.0, 0.0),
        embedding_version=runtime.config.embedding_version,
        exclude_news_ids=frozenset(), limit=10,
    )


@pytest.mark.asyncio
async def test_fixed_business_clock_populates_all_three_tiers_and_deduplicates_news(runtime):
    ages = {
        VectorTier.HOT: runtime.config.hot_max_age_seconds / 2,
        VectorTier.WARM: runtime.config.hot_max_age_seconds,
        VectorTier.COLD: runtime.config.cold_min_age_seconds,
    }
    for tier, age in ages.items():
        job = await index_document(runtime, document(tier.value, age_seconds=age))
        assert job["target_tier"] == tier.value
        assert job["status"] == "complete"
        layer = await hits(runtime, tier=tier)
        assert len(layer) > 1  # Multiple chunks still produce a single news result.
        assert {hit.news_id for hit in layer} == {tier.value}
    result = await runtime.query.search(query_text="新闻", tenant_id=TENANT, limit=3)
    assert {hit.news_id for hit in result["hits"]} == {"hot", "warm", "cold"}
    trace = result["trace"]
    assert trace["returned_news"] == 3
    assert set(trace["tiers"]) == {"hot", "warm", "cold"}
    assert trace["total_ms"] >= trace["embedding_ms"] >= 0
    assert trace["policy_version"] == runtime.config.policy_version


@pytest.mark.asyncio
async def test_correction_blocks_old_revision_at_accept_and_recovers_after_new_activation(runtime):
    old = await index_document(runtime)
    replacement = await runtime.service.accept(
        tenant_id=TENANT, document=document(version=2, text="已经更正的新正文与新事实"), now=NOW,
    )
    assert await hits(runtime) == []
    assert await hits(runtime, index=runtime.raw_index)  # Old physical rows still exist.
    reopened = runtime.reopen()
    await reopened.process_job(tenant_id=TENANT, job_id=replacement["job_id"])
    active = await hits(runtime)
    assert active and {hit.content_version for hit in active} == {2}
    assert not await runtime.raw_index.contains(
        tier=VectorTier.HOT, chunk_ids=[r["chunk_id"] for r in old["records"]],
    )
    assert (await reopened.get_document_status(
        tenant_id=TENANT, document_id="document-story",
    ))["status"] == "ready"


@pytest.mark.asyncio
async def test_withdrawal_blocks_all_tiers_before_physical_cleanup_and_cannot_be_replayed(runtime):
    await index_document(runtime)
    row = await runtime.ledger.get_news(tenant_id=TENANT, news_id="story")
    migration = await runtime.ledger.request_migration(
        tenant_id=TENANT, news_id="story", target_tier="warm", expected_generation=row["generation"],
    )
    await runtime.service.process_job(tenant_id=TENANT, job_id=migration["job_id"])
    # Recreate a delayed old SDK write in the HOT collection.
    old_jobs = await runtime.ledger.obsolete_jobs(tenant_id=TENANT, news_id="story")
    from app.knowledge.tier_lifecycle import record_from_dict
    await runtime.raw_index.upsert(
        tier=VectorTier.HOT, records=[record_from_dict(record) for record in old_jobs[0]["records"]],
    )
    withdrawn = await runtime.service.retract(tenant_id=TENANT, news_id="story", source_revision=2)
    for tier in VectorTier:
        assert await hits(runtime, tier=tier) == []
    await runtime.reopen().process_job(tenant_id=TENANT, job_id=withdrawn["job_id"])
    for tier in VectorTier:
        assert await hits(runtime, tier=tier, index=runtime.raw_index) == []
    with pytest.raises(ValueError, match="older"):
        await runtime.service.accept(tenant_id=TENANT, document=document(), now=NOW)
    await runtime.service.process_job(tenant_id=TENANT, job_id=migration["job_id"])
    assert (await runtime.ledger.get_news(tenant_id=TENANT, news_id="story"))["status"] == "retracted"


@pytest.mark.asyncio
async def test_migration_keeps_ready_source_until_target_verified_then_cleans_source(runtime):
    old = await index_document(runtime)
    migration = await runtime.ledger.request_migration(
        tenant_id=TENANT, news_id="story", target_tier="cold",
    )
    assert await hits(runtime)

    class CheckingIndex:
        async def upsert(self, **kwargs):
            return await runtime.raw_index.upsert(**kwargs)

        async def verify_records(self, **kwargs):
            row = await runtime.ledger.get_news(tenant_id=TENANT, news_id="story")
            assert row["active_job_id"] == old["job_id"]
            assert await hits(runtime, tier=VectorTier.HOT)
            assert await hits(runtime, tier=VectorTier.COLD) == []
            return await runtime.raw_index.verify_records(**kwargs)

        async def delete(self, **kwargs):
            row = await runtime.ledger.get_news(tenant_id=TENANT, news_id="story")
            assert row["active_job_id"] == migration["job_id"]
            return await runtime.raw_index.delete(**kwargs)

    await runtime.reopen(CheckingIndex()).process_job(tenant_id=TENANT, job_id=migration["job_id"])
    assert await hits(runtime, tier=VectorTier.HOT) == []
    assert await hits(runtime, tier=VectorTier.HOT, index=runtime.raw_index) == []
    assert await hits(runtime, tier=VectorTier.COLD)


@pytest.mark.asyncio
async def test_crash_after_target_write_before_activation_resumes_without_reembedding(runtime):
    old = await index_document(runtime)
    migration = await runtime.ledger.request_migration(
        tenant_id=TENANT, news_id="story", target_tier="warm",
    )

    class CrashAfterWrite:
        async def upsert(self, **kwargs):
            return await runtime.raw_index.upsert(**kwargs)

        async def verify_records(self, **kwargs):
            raise TimeoutError("simulated lost response")

    with pytest.raises(TimeoutError):
        await runtime.reopen(CrashAfterWrite()).process_job(tenant_id=TENANT, job_id=migration["job_id"])
    calls = runtime.embedding.calls
    job = await runtime.ledger.get_job(tenant_id=TENANT, job_id=migration["job_id"])
    assert job["records"] and job["status"] == "retry"
    row = await runtime.ledger.get_news(tenant_id=TENANT, news_id="story")
    assert row["active_job_id"] == old["job_id"]
    assert await hits(runtime)
    assert await hits(runtime, tier=VectorTier.WARM) == []
    assert await hits(runtime, tier=VectorTier.WARM, index=runtime.raw_index)
    await runtime.reopen().process_job(tenant_id=TENANT, job_id=migration["job_id"])
    assert runtime.embedding.calls == calls
    assert await hits(runtime, tier=VectorTier.WARM)
    assert not await hits(runtime, tier=VectorTier.HOT, index=runtime.raw_index)


@pytest.mark.asyncio
async def test_new_revision_accepted_during_old_verification_fences_old_activation(runtime):
    old = await runtime.service.accept(tenant_id=TENANT, document=document(), now=NOW)
    newer = []

    class RevisionDuringVerification:
        async def upsert(self, **kwargs):
            return await runtime.raw_index.upsert(**kwargs)

        async def verify_records(self, **kwargs):
            if not newer:
                newer.append(await runtime.service.accept(
                    tenant_id=TENANT, document=document(version=2), now=NOW,
                ))
            return await runtime.raw_index.verify_records(**kwargs)

        async def delete(self, **kwargs):
            return await runtime.raw_index.delete(**kwargs)

    await runtime.reopen(RevisionDuringVerification()).process_job(tenant_id=TENANT, job_id=old["job_id"])
    assert await hits(runtime) == []
    row = await runtime.ledger.get_news(tenant_id=TENANT, news_id="story")
    assert row["active_job_id"] is None and row["source_revision"] == 2
    await runtime.service.process_job(tenant_id=TENANT, job_id=newer[0]["job_id"])
    assert {hit.content_version for hit in await hits(runtime)} == {2}
    # A later old-job replay cannot delete the now-active v2 projection.
    await runtime.service.process_job(tenant_id=TENANT, job_id=old["job_id"])
    assert {hit.content_version for hit in await hits(runtime)} == {2}


@pytest.mark.asyncio
async def test_final_query_gate_reconciles_migration_between_layer_reads(runtime):
    await index_document(runtime)
    migrated = False

    class MigrationBetweenReads:
        async def search(self, **kwargs):
            nonlocal migrated
            if kwargs["tier"] == VectorTier.WARM and not migrated:
                migrated = True
                migration = await runtime.ledger.request_migration(
                    tenant_id=TENANT, news_id="story", target_tier="warm",
                )
                await runtime.service.process_job(tenant_id=TENANT, job_id=migration["job_id"])
            return await runtime.raw_index.search(**kwargs)

    query = LifecycleKnowledgeSearch(
        index=MigrationBetweenReads(), ledger=runtime.ledger,
        embedder=runtime.embedding, config=runtime.config,
    )
    result = await query.search(query_text="新闻", tenant_id=TENANT)
    assert len(result["hits"]) == 1
    assert result["hits"][0].tier == VectorTier.WARM
    assert result["scores"]["story"] == runtime.config.query_weights[VectorTier.WARM] / (runtime.config.query_rrf_k + 1)


@pytest.mark.asyncio
async def test_scope_prevents_cross_tenant_projection_and_task_access(runtime):
    first = await index_document(runtime, tenant_id=TENANT)
    second = await index_document(runtime, document(version=2), tenant_id="tenant-other")
    assert {hit.content_version for hit in await hits(runtime)} == {1}
    assert {hit.content_version for hit in await hits(runtime, tenant_id="tenant-other")} == {2}
    assert await runtime.ledger.get_job(tenant_id=TENANT, job_id=second["job_id"]) is None
    with pytest.raises(ValueError, match="unknown"):
        await runtime.service.process_job(tenant_id=TENANT, job_id=second["job_id"])
    await runtime.service.retract(tenant_id="tenant-other", news_id="story", source_revision=3)
    assert await hits(runtime)
    assert await hits(runtime, tenant_id="tenant-other") == []
    assert (await runtime.ledger.get_news(tenant_id=TENANT, news_id="story"))["active_job_id"] == first["job_id"]


@pytest.mark.asyncio
async def test_clock_scan_and_expiring_reheat_produce_recoverable_migrations(runtime):
    await index_document(runtime)
    future = NOW + timedelta(seconds=runtime.config.cold_min_age_seconds)
    await runtime.service.scan_page(now=future)
    row = await runtime.ledger.get_news(tenant_id=TENANT, news_id="story")
    assert row["desired_tier"] == "cold" and row["active_tier"] == "hot"
    await runtime.service.process_job(tenant_id=TENANT, job_id=row["current_job_id"])
    reheat = await runtime.service.reheat(tenant_id=TENANT, news_id="story", now=future)
    await runtime.service.process_job(tenant_id=TENANT, job_id=reheat["job_id"])
    await runtime.service.scan_page(now=future + timedelta(seconds=runtime.config.reheat_hold_seconds / 2))
    row = await runtime.ledger.get_news(tenant_id=TENANT, news_id="story")
    assert row["desired_tier"] == "hot"
    await runtime.service.scan_page(now=future + timedelta(seconds=runtime.config.reheat_hold_seconds))
    row = await runtime.ledger.get_news(tenant_id=TENANT, news_id="story")
    assert row["desired_tier"] == "cold"
    await runtime.reopen().process_job(tenant_id=TENANT, job_id=row["current_job_id"])
    assert await hits(runtime, tier=VectorTier.COLD)


@pytest.mark.asyncio
async def test_losing_attempt_does_not_delete_projection_activated_by_another_attempt(runtime):
    job = await runtime.service.accept(tenant_id=TENANT, document=document(), now=NOW)

    class ConcurrentActivationThenCleanupFailure:
        async def upsert(self, **kwargs):
            return await runtime.raw_index.upsert(**kwargs)

        async def verify_records(self, **kwargs):
            assert await runtime.raw_index.verify_records(**kwargs)
            # Another delivery activated the same immutable job, then exhausted
            # its cleanup retries. The delayed delivery now loses activation.
            assert await runtime.ledger.activate(tenant_id=TENANT, job_id=job["job_id"])
            await runtime.ledger.record_error(
                tenant_id=TENANT, job_id=job["job_id"], error="CleanupTimeout", retryable=False,
            )
            return True

        async def delete(self, **kwargs):
            raise AssertionError("the still-active projection must not be deleted")

    result = await runtime.reopen(ConcurrentActivationThenCleanupFailure()).process_job(
        tenant_id=TENANT, job_id=job["job_id"],
    )
    row = await runtime.ledger.get_news(tenant_id=TENANT, news_id="story")
    assert row["status"] == "active" and row["active_job_id"] == job["job_id"]
    assert await hits(runtime)
    assert result["status"] == "failed"
