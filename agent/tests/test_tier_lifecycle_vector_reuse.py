"""Migration reuses an eligible immutable projection, never an older revision.

The shared SQL/runtime fixture exercises the production service and ledger with
an isolated SQLite database. Scheduled interleavings verify activation fences;
they do not claim to reproduce PostgreSQL transaction lock contention.
"""

import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import update

from app.modules.knowledge.tier_lifecycle import KnowledgeLifecycleService
from app.modules.knowledge.tier_lifecycle_config import KnowledgeLifecycleConfig
from app.modules.knowledge.tier_lifecycle_store import _jobs, _news
from app.model_runtime.core import EmbeddingResult
from app.retrieval.tiered_vector import VectorTier
from tests.test_tier_lifecycle_runtime import (
    NOW, TENANT, document, hits, index_document, runtime,
)


async def migrate(runtime, tier, *, config=None):
    return await runtime.ledger.request_migration(
        tenant_id=TENANT, news_id="story", target_tier=tier,
        config=config.model_dump(mode="json") if config else None,
    )


@pytest.mark.asyncio
async def test_three_tier_round_trip_reuses_vectors_with_fresh_projection_ids(runtime):
    source = await index_document(runtime)
    original_vectors = [record["vector"] for record in source["records"]]
    calls = runtime.embedding.calls
    seen_ids = set(source["chunk_ids"])
    for tier in ("warm", "cold", "hot"):
        migration = await migrate(runtime, tier)
        await runtime.reopen().process_job(tenant_id=TENANT, job_id=migration["job_id"])
        saved = await runtime.ledger.get_job(tenant_id=TENANT, job_id=migration["job_id"])
        assert runtime.embedding.calls == calls
        assert [record["vector"] for record in saved["records"]] == original_vectors
        assert not seen_ids.intersection(saved["chunk_ids"])
        seen_ids.update(saved["chunk_ids"])
        assert await hits(runtime, tier=VectorTier(tier))
        assert not await runtime.raw_index.contains(
            tier=VectorTier(source["target_tier"]), chunk_ids=source["chunk_ids"],
        )
        source = saved


@pytest.mark.asyncio
async def test_policy_and_batch_changes_do_not_force_reembedding(runtime):
    await index_document(runtime)
    calls = runtime.embedding.calls
    cfg = KnowledgeLifecycleConfig.model_validate(runtime.config.model_dump() | {
        "policy_version": "new-age-policy", "hot_max_age_seconds": 60.0,
        "embedding_batch_size": 1, "vector_batch_size": 1,
    })
    migration = await migrate(runtime, "warm", config=cfg)
    await runtime.service.process_job(tenant_id=TENANT, job_id=migration["job_id"])
    assert runtime.embedding.calls == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"chunk_chars": 7}, {"chunk_overlap": 0}, {"chunking_version": "chars-v2"},
    {"embedding_version": "embedding-v2"}, {"expected_embedding_dimensions": 3},
])
async def test_incompatible_embedding_or_chunk_contract_builds_new_vectors(runtime, change):
    old = await index_document(runtime)
    cfg = KnowledgeLifecycleConfig.model_validate(runtime.config.model_dump() | change)

    class NewEmbedding:
        calls = 0

        async def embed(self, request):
            self.calls += 1
            return EmbeddingResult(model_version=request.model_route,
                vectors=tuple(tuple(0.25 for _ in range(cfg.expected_embedding_dimensions))
                              for _ in request.texts))

    embedding = NewEmbedding()
    service = KnowledgeLifecycleService(ledger=runtime.ledger, index=runtime.raw_index,
                                        embedding=embedding, config=cfg)
    migration = await migrate(runtime, "warm", config=cfg)
    await service.process_job(tenant_id=TENANT, job_id=migration["job_id"])
    saved = await runtime.ledger.get_job(tenant_id=TENANT, job_id=migration["job_id"])
    assert embedding.calls > 0
    assert all(len(record["vector"]) == cfg.expected_embedding_dimensions for record in saved["records"])
    assert all(record["vector"] != old["records"][0]["vector"] for record in saved["records"])


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [
    "missing_chunk", "missing_manifest", "vector_dimension", "vector_infinite", "vector_bool",
    "chunk_id", "chunk_index", "tenant_id", "news_id", "content_version", "embedding_version",
    "title", "excerpt", "source_url", "publish_time", "source_payload", "source_revision",
    "source_scope", "body_hash", "missing_source",
])
async def test_invalid_source_snapshot_falls_back_to_embedding(runtime, damage):
    old = await index_document(runtime)
    calls = runtime.embedding.calls
    changed = deepcopy(old)
    values = {}
    news_values = {}
    if damage == "missing_chunk":
        values["records"] = changed["records"][:-1]
    elif damage == "missing_manifest":
        values["chunk_ids"] = changed["chunk_ids"][:-1]
    elif damage.startswith("vector_"):
        changed["records"][0]["vector"] = {
            "vector_dimension": [1.0], "vector_infinite": [float("inf"), 0.0],
            "vector_bool": [True, False],
        }[damage]
        values["records"] = changed["records"]
    elif damage == "source_payload":
        changed["payload"]["body"] = "different body"
        values["payload"] = changed["payload"]
    elif damage == "source_revision":
        values["source_revision"] = 999
    elif damage == "source_scope":
        values["news_id"] = "another-news"
    elif damage == "body_hash":
        news_values["content_hash"] = "wrong-hash"
    elif damage == "missing_source":
        news_values["active_job_id"] = "missing-source-id"
    else:
        changed["records"][0][damage] = 999 if damage in ("chunk_index", "content_version") else "wrong"
        values["records"] = changed["records"]
    async with runtime.database.session() as session:
        if values:
            await session.execute(update(_jobs).where(_jobs.c.job_id == old["job_id"]).values(**values))
        if news_values:
            await session.execute(update(_news).where(_news.c.tenant_id == TENANT,
                _news.c.news_id == "story").values(**news_values))
    migration = await migrate(runtime, "warm")
    await runtime.service.process_job(tenant_id=TENANT, job_id=migration["job_id"])
    assert runtime.embedding.calls > calls
    row = await runtime.ledger.get_news(tenant_id=TENANT, news_id="story")
    assert row["active_job_id"] == migration["job_id"]
    assert await hits(runtime, tier=VectorTier.WARM)


@pytest.mark.asyncio
async def test_new_content_revision_is_never_reused_even_if_text_is_identical(runtime):
    old_doc = document()
    await index_document(runtime, old_doc)
    calls = runtime.embedding.calls
    new_doc = document(version=2)
    new_doc = type(new_doc)(document_id=new_doc.document_id, title=old_doc.title, text=old_doc.text,
        media_type=new_doc.media_type, text_source=new_doc.text_source,
        source_url=new_doc.source_url, metadata=new_doc.metadata)
    new = await runtime.service.accept(tenant_id=TENANT, document=new_doc, now=NOW)
    await runtime.service.process_job(tenant_id=TENANT, job_id=new["job_id"])
    assert runtime.embedding.calls > calls
    assert {hit.content_version for hit in await hits(runtime)} == {2}


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["correct", "withdraw", "supersede"])
async def test_change_after_source_snapshot_cannot_activate_old_migration(runtime, monkeypatch, event):
    await index_document(runtime)
    migration = await migrate(runtime, "warm")
    original = runtime.service.ledger.reusable_source_job
    replacement = []

    async def snapshot_then_change(**kwargs):
        source = await original(**kwargs)
        assert source is not None
        if event == "correct":
            replacement.append(await runtime.service.accept(
                tenant_id=TENANT, document=document(version=2), now=NOW))
        elif event == "withdraw":
            replacement.append(await runtime.service.retract(
                tenant_id=TENANT, news_id="story", source_revision=2))
        else:
            replacement.append(await migrate(runtime, "cold"))
        return source

    monkeypatch.setattr(runtime.service.ledger, "reusable_source_job", snapshot_then_change)
    await runtime.service.process_job(tenant_id=TENANT, job_id=migration["job_id"])
    row = await runtime.ledger.get_news(tenant_id=TENANT, news_id="story")
    assert row["current_job_id"] == replacement[0]["job_id"]
    assert row["active_job_id"] != migration["job_id"]
    assert not await hits(runtime, tier=VectorTier.WARM)
    assert not await hits(runtime, tier=VectorTier.WARM, index=runtime.raw_index)
    await runtime.reopen().process_job(tenant_id=TENANT, job_id=replacement[0]["job_id"])
    if event == "correct":
        assert {hit.content_version for hit in await hits(runtime)} == {2}
    elif event == "withdraw":
        assert not await hits(runtime)
    else:
        assert await hits(runtime, tier=VectorTier.COLD)


@pytest.mark.asyncio
async def test_failed_target_write_restarts_from_saved_copy_without_embedding(runtime):
    source = await index_document(runtime)
    migration = await migrate(runtime, "warm")
    calls = runtime.embedding.calls

    class FailingIndex:
        async def upsert(self, **kwargs):
            saved = await runtime.ledger.get_job(tenant_id=TENANT, job_id=migration["job_id"])
            assert saved["records"]
            assert not set(source["chunk_ids"]).intersection(saved["chunk_ids"])
            raise TimeoutError("target unavailable")

    with pytest.raises(TimeoutError):
        await runtime.reopen(FailingIndex()).process_job(tenant_id=TENANT, job_id=migration["job_id"])
    assert await hits(runtime, tier=VectorTier.HOT)
    await runtime.reopen().process_job(tenant_id=TENANT, job_id=migration["job_id"])
    assert runtime.embedding.calls == calls
    assert await hits(runtime, tier=VectorTier.WARM)


@pytest.mark.asyncio
async def test_invalid_cached_target_fails_closed_without_fresh_model_call(runtime):
    await index_document(runtime)
    migration = await migrate(runtime, "warm")
    copied = await runtime.service._records(migration)
    calls = runtime.embedding.calls
    corrupted = deepcopy(copied)
    corrupted[0]["vector"] = [1.0]
    async with runtime.database.session() as session:
        await session.execute(update(_jobs).where(_jobs.c.job_id == migration["job_id"])
                              .values(records=corrupted))
    with pytest.raises(ValueError, match="immutable contract"):
        await runtime.service.process_job(tenant_id=TENANT, job_id=migration["job_id"])
    assert runtime.embedding.calls == calls
    assert await hits(runtime, tier=VectorTier.HOT)
    assert not await hits(runtime, tier=VectorTier.WARM)
    assert (await runtime.ledger.get_job(tenant_id=TENANT, job_id=migration["job_id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_concurrent_attempts_reuse_one_snapshot_and_keep_active_target(runtime, monkeypatch):
    await index_document(runtime)
    migration = await migrate(runtime, "warm")
    calls = runtime.embedding.calls
    original = runtime.service.ledger.reusable_source_job
    both_snapshots = asyncio.Event()
    arrivals = 0

    async def synchronized_snapshot(**kwargs):
        nonlocal arrivals
        source = await original(**kwargs)
        assert source is not None
        arrivals += 1
        if arrivals == 2:
            both_snapshots.set()
        await asyncio.wait_for(both_snapshots.wait(), timeout=2)
        return source

    monkeypatch.setattr(runtime.service.ledger, "reusable_source_job", synchronized_snapshot)
    results = await asyncio.gather(*(runtime.service.process_job(
        tenant_id=TENANT, job_id=migration["job_id"]) for _ in range(2)))
    assert all(result["status"] == "complete" for result in results)
    assert runtime.embedding.calls == calls
    saved = await runtime.ledger.get_job(tenant_id=TENANT, job_id=migration["job_id"])
    assert len(saved["chunk_ids"]) == len(set(saved["chunk_ids"]))
    assert await hits(runtime, tier=VectorTier.WARM)
    assert not await hits(runtime, tier=VectorTier.HOT)
