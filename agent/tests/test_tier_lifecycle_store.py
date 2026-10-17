"""Lifecycle safety tests execute real SQL statements in isolated transactions.

SQLite is only a deterministic ledger test harness; the optional PostgreSQL test
uses a fresh schema to exercise advisory-lock serialization with real async IO.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.modules.knowledge.tier_lifecycle_store import (
    TierLifecycleStore, _metadata, projection_chunk_id,
)
from app.retrieval.tiered_vector import VectorSearchHit, VectorTier


NOW = datetime(2026, 10, 15, tzinfo=UTC)
CONFIG = {"embedding_version": "embedding-v1", "chunking_version": "chars-v1",
          "chunk_chars": 500, "chunk_overlap": 50}


class TransactionalDatabase:
    def __init__(self):
        self.engine = create_engine("sqlite://")
        _metadata.create_all(self.engine)

    @asynccontextmanager
    async def session(self):
        with Session(self.engine) as session:
            async def execute(*args, **kwargs):
                return session.execute(*args, **kwargs)
            try:
                yield SimpleNamespace(execute=execute)
                session.commit()
            except Exception:
                session.rollback()
                raise


@pytest.fixture
def store():
    database = TransactionalDatabase()
    yield TierLifecycleStore(database=database)
    database.engine.dispose()


async def accept(store, *, tenant="tenant-a", news="one", revision=1, **overrides):
    fields = dict(tenant_id=tenant, news_id=news, source_revision=revision,
                  title="新闻标题", body=f"新闻正文 v{revision}", publish_time=NOW,
                  source_url=None, desired_tier="hot", config=CONFIG)
    return await store.accept_content(**(fields | overrides))


def records(job, *, vector=(1.0, 0.0)):
    return [dict(chunk_id=projection_chunk_id(job["job_id"], 0),
                 chunk_index=0, tenant_id=job["tenant_id"], news_id=job["news_id"],
                 content_version=job["source_revision"],
                 embedding_version=job["config"]["embedding_version"],
                 title=job["payload"]["title"], excerpt=job["payload"]["body"],
                 source_url=None, publish_time=NOW.isoformat(), vector=list(vector))]


def hit(job, **overrides):
    fields = dict(chunk_id=projection_chunk_id(job["job_id"], 0), news_id=job["news_id"],
                  title="title", excerpt="excerpt", source_url=None, publish_time=NOW,
                  raw_score=1.0, tier=VectorTier(job["target_tier"]),
                  content_version=job["source_revision"],
                  embedding_version=job["config"]["embedding_version"])
    return VectorSearchHit(**(fields | overrides))


async def ready(store, job):
    await store.save_records(tenant_id=job["tenant_id"], job_id=job["job_id"], records=records(job))
    assert await store.activate(tenant_id=job["tenant_id"], job_id=job["job_id"])
    await store.complete(tenant_id=job["tenant_id"], job_id=job["job_id"])


@pytest.mark.asyncio
async def test_accept_is_durable_and_same_revision_checks_content(store):
    job = await accept(store, document_id="doc-one")
    assert job["status"] == "pending"
    assert (await accept(store, document_id="doc-one"))["job_id"] == job["job_id"]
    reopened = TierLifecycleStore(database=store._database)
    assert (await reopened.get_job(tenant_id="tenant-a", job_id=job["job_id"])) == job
    assert (await reopened.get_document(tenant_id="tenant-a", document_id="doc-one"))["status"] == "pending"
    with pytest.raises(ValueError, match="conflicting"):
        await accept(store, body="different", document_id="doc-one")
    with pytest.raises(ValueError, match="identity cannot change"):
        await accept(store, revision=2)
    assert len(await store.list_pending()) == 1


@pytest.mark.asyncio
async def test_source_revisions_support_timestamp_sized_signed_64_bit_values(store):
    job = await accept(store, revision=1_760_000_000_000)
    assert job["source_revision"] == 1_760_000_000_000
    with pytest.raises(ValueError, match="64-bit"):
        await accept(store, revision=2**63)


@pytest.mark.asyncio
async def test_correction_immediately_fences_old_projection_and_old_worker(store):
    old = await accept(store)
    await ready(store, old)
    assert await store.filter_hits(tenant_id="tenant-a", hits=[hit(old)]) == [hit(old)]
    new = await accept(store, revision=2)
    assert await store.filter_hits(tenant_id="tenant-a", hits=[hit(old)]) == []
    assert not await store.activate(tenant_id="tenant-a", job_id=old["job_id"])
    await ready(store, new)
    hits = [hit(old), hit(new), hit(new, content_version=1), hit(new, tier=VectorTier.COLD),
            hit(new, embedding_version="other-model"), hit(new, chunk_id="foreign")]
    assert await store.filter_hits(tenant_id="tenant-a", hits=hits) == [hit(new)]
    assert await store.filter_hits(tenant_id="tenant-b", hits=hits) == []
    with pytest.raises(ValueError, match="older"):
        await accept(store)


@pytest.mark.asyncio
async def test_migration_preserves_predecessor_until_activation_and_uses_distinct_ids(store):
    old = await accept(store)
    await ready(store, old)
    new = await store.request_migration(tenant_id="tenant-a", news_id="one", target_tier="warm",
                                        expected_generation=1)
    assert new["generation"] == 2
    assert await store.filter_hits(tenant_id="tenant-a", hits=[hit(old), hit(new)]) == [hit(old)]
    assert await store.obsolete_jobs(tenant_id="tenant-a", news_id="one") == []
    await ready(store, new)
    assert await store.filter_hits(tenant_id="tenant-a", hits=[hit(old), hit(new)]) == [hit(new)]
    assert [job["job_id"] for job in await store.obsolete_jobs(tenant_id="tenant-a", news_id="one")] == [old["job_id"]]
    assert (await accept(store))["job_id"] == old["job_id"]
    returned = await store.request_migration(tenant_id="tenant-a", news_id="one", target_tier="hot")
    assert projection_chunk_id(returned["job_id"], 0) != projection_chunk_id(old["job_id"], 0)


@pytest.mark.asyncio
async def test_stale_age_scan_cannot_override_new_content_or_manual_hold(store):
    old = await accept(store)
    await ready(store, old)
    held = await store.request_migration(tenant_id="tenant-a", news_id="one", target_tier="hot",
                                         expected_generation=1, hold_until=NOW + timedelta(days=1))
    assert held["generation"] == 2
    assert await store.request_migration(tenant_id="tenant-a", news_id="one", target_tier="cold",
                                          expected_generation=1) is None
    await accept(store, revision=2)
    assert await store.request_migration(tenant_id="tenant-a", news_id="one", target_tier="cold",
                                          expected_generation=2) is None


@pytest.mark.asyncio
async def test_withdrawal_fences_late_indexing_and_retains_cleanup_manifest(store):
    old = await accept(store)
    retracted = await store.retract(tenant_id="tenant-a", news_id="one", source_revision=2)
    # External embedding/write may return after the withdrawal job already ran.
    await store.complete(tenant_id="tenant-a", job_id=old["job_id"])
    await store.save_records(tenant_id="tenant-a", job_id=old["job_id"], records=records(old))
    assert not await store.activate(tenant_id="tenant-a", job_id=old["job_id"])
    assert await store.filter_hits(tenant_id="tenant-a", hits=[hit(old)]) == []
    assert {j["job_id"] for j in await store.list_pending()} == {old["job_id"], retracted["job_id"]}
    assert [j["job_id"] for j in await store.obsolete_jobs(tenant_id="tenant-a", news_id="one")] == [old["job_id"]]
    assert not await store.list_news()
    assert len(await store.list_news(include_retracted=True)) == 1
    assert await store.request_migration(tenant_id="tenant-a", news_id="one", target_tier="cold") is None
    assert (await store.retract(tenant_id="tenant-a", news_id="one", source_revision=2))["job_id"] == retracted["job_id"]
    with pytest.raises(ValueError, match="conflicting"):
        await accept(store, revision=2)


@pytest.mark.asyncio
async def test_first_seen_tombstone_prevents_out_of_order_news_creation(store):
    await store.retract(tenant_id="tenant-a", news_id="one", source_revision=3)
    with pytest.raises(ValueError, match="older"):
        await accept(store, revision=2)
    assert not await store.list_news()
    restored = await accept(store, revision=4, document_id="source-doc-one")
    assert restored["source_revision"] == 4
    assert (await store.get_document(tenant_id="tenant-a", document_id="source-doc-one"))["news_id"] == "one"


@pytest.mark.asyncio
async def test_saved_records_are_immutable_first_success_wins(store):
    config = dict(CONFIG)
    job = await accept(store, config=config)
    config["embedding_version"] = "mutated"
    first = await store.save_records(tenant_id="tenant-a", job_id=job["job_id"], records=records(job))
    second = await store.save_records(tenant_id="tenant-a", job_id=job["job_id"], records=records(job, vector=(0, 1)))
    assert first["records"] == second["records"]
    assert second["config"]["embedding_version"] == "embedding-v1"
    assert second["chunk_ids"] == [projection_chunk_id(job["job_id"], 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"chunk_id": "foreign"}, {"tenant_id": "tenant-b"}, {"content_version": 8},
    {"embedding_version": "other"}, {"vector": []}, {"vector": [float("inf")]},
])
async def test_invalid_projection_never_reaches_durable_manifest(store, changes):
    job = await accept(store)
    rows = records(job)
    rows[0].update(changes)
    with pytest.raises(ValueError):
        await store.save_records(tenant_id="tenant-a", job_id=job["job_id"], records=rows)
    assert not (await store.get_job(tenant_id="tenant-a", job_id=job["job_id"]))["records"]


@pytest.mark.asyncio
async def test_job_scope_and_status_monotonic_after_completion(store):
    job = await accept(store)
    assert await store.get_job(tenant_id="tenant-b", job_id=job["job_id"]) is None
    await store.record_error(tenant_id="tenant-a", job_id=job["job_id"], error="temporarily unavailable")
    assert (await store.get_job(tenant_id="tenant-a", job_id=job["job_id"]))["attempts"] == 1
    await ready(store, job)
    await store.record_error(tenant_id="tenant-a", job_id=job["job_id"], error="old worker failure")
    assert await store.activate(tenant_id="tenant-a", job_id=job["job_id"])
    done = await store.get_job(tenant_id="tenant-a", job_id=job["job_id"])
    assert done["status"] == done["phase"] == "complete"
    assert done["last_error"] is None
    assert await store.list_pending() == []


@pytest.mark.asyncio
async def test_keyset_pagination_visits_every_scope_and_job(store):
    expected = []
    for tenant, news in [("a", "two"), ("a", "one"), ("b", "one")]:
        expected.append(await accept(store, tenant=tenant, news=news))
    seen, cursor = [], None
    while page := await store.list_news(limit=1, after_key=cursor):
        seen.append((page[0]["tenant_id"], page[0]["news_id"]))
        cursor = seen[-1]
    assert seen == [("a", "one"), ("a", "two"), ("b", "one")]
    jobs, cursor = [], None
    while page := await store.list_pending(limit=1, after_job_id=cursor):
        jobs.append(page[0]["job_id"])
        cursor = jobs[-1]
    assert set(jobs) == {job["job_id"] for job in expected}


@pytest.mark.asyncio
async def test_explicit_retry_renews_failed_job_and_fences_its_late_attempt(store):
    old = await accept(store)
    await store.record_error(tenant_id="tenant-a", job_id=old["job_id"], error="exhausted", retryable=False)
    new = await store.retry_job(tenant_id="tenant-a", job_id=old["job_id"])
    assert new["job_id"] != old["job_id"]
    assert new["generation"] == old["generation"] + 1
    assert new["config"] == old["config"] and new["payload"] == old["payload"]
    assert (await accept(store))["job_id"] == new["job_id"]
    await store.save_records(tenant_id="tenant-a", job_id=old["job_id"], records=records(old))
    assert not await store.activate(tenant_id="tenant-a", job_id=old["job_id"])
    with pytest.raises(ValueError, match="current failed"):
        await store.retry_job(tenant_id="tenant-a", job_id=old["job_id"])
    with pytest.raises(ValueError, match="current failed"):
        await store.retry_job(tenant_id="tenant-a", job_id=new["job_id"])
    await ready(store, new)


@pytest.mark.asyncio
async def test_terminal_failure_cannot_be_cleared_or_activated_by_late_attempt(store):
    job = await accept(store)
    await store.save_records(tenant_id="tenant-a", job_id=job["job_id"], records=records(job))
    await store.record_error(tenant_id="tenant-a", job_id=job["job_id"], error="exhausted", retryable=False)
    assert not await store.activate(tenant_id="tenant-a", job_id=job["job_id"])
    await store.record_error(tenant_id="tenant-a", job_id=job["job_id"], error="late retry", retryable=True)
    await store.complete(tenant_id="tenant-a", job_id=job["job_id"])
    terminal = await store.get_job(tenant_id="tenant-a", job_id=job["job_id"])
    assert terminal["status"] == "failed" and terminal["last_error"] == "exhausted"
    assert not await store.filter_hits(tenant_id="tenant-a", hits=[hit(job)])


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_active_projection_searchable(store):
    job = await accept(store)
    await store.save_records(tenant_id="tenant-a", job_id=job["job_id"], records=records(job))
    assert await store.activate(tenant_id="tenant-a", job_id=job["job_id"])
    await store.record_error(tenant_id="tenant-a", job_id=job["job_id"], error="cleanup exhausted", retryable=False)
    assert await store.filter_hits(tenant_id="tenant-a", hits=[hit(job)]) == [hit(job)]


@pytest.mark.asyncio
async def test_retry_failed_migration_keeps_ready_source_and_retry_retract_stays_hidden(store):
    old = await accept(store)
    await ready(store, old)
    migration = await store.request_migration(tenant_id="tenant-a", news_id="one", target_tier="cold")
    await store.record_error(tenant_id="tenant-a", job_id=migration["job_id"], error="exhausted", retryable=False)
    retried = await store.retry_job(tenant_id="tenant-a", job_id=migration["job_id"])
    assert await store.filter_hits(tenant_id="tenant-a", hits=[hit(old), hit(retried)]) == [hit(old)]
    withdrawal = await store.retract(tenant_id="tenant-a", news_id="one", source_revision=2)
    await store.record_error(tenant_id="tenant-a", job_id=withdrawal["job_id"], error="exhausted", retryable=False)
    await store.retry_job(tenant_id="tenant-a", job_id=withdrawal["job_id"])
    assert not await store.filter_hits(tenant_id="tenant-a", hits=[hit(old)])


@pytest.mark.asyncio
async def test_real_postgres_concurrent_same_revision_has_one_accepted_job():
    url = os.environ.get("NEWSAGENT_TIER_LIFECYCLE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set explicit isolated PostgreSQL test URL to exercise real locking")
    schema = "tier_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class TestDatabase:
        @asynccontextmanager
        async def session(self):
            async with factory.begin() as session:
                yield session

    database = TestDatabase()
    database.engine = engine
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_metadata.create_all)
        ledger = TierLifecycleStore(database=database)
        results = await asyncio.gather(*(accept(ledger) for _ in range(8)))
        assert len({job["job_id"] for job in results}) == 1
        assert len(await ledger.list_pending()) == 1
        current = results[0]
        await ready(ledger, current)
        outcomes = await asyncio.gather(
            ledger.request_migration(tenant_id="tenant-a", news_id="one", target_tier="cold",
                                     expected_generation=1),
            ledger.retract(tenant_id="tenant-a", news_id="one", source_revision=2),
        )
        assert (await ledger.get_news(tenant_id="tenant-a", news_id="one"))["status"] == "retracted"
        assert not await ledger.filter_hits(tenant_id="tenant-a", hits=[hit(current)])
        if outcomes[0]:
            await ledger.save_records(tenant_id="tenant-a", job_id=outcomes[0]["job_id"],
                                      records=records(outcomes[0]))
            assert not await ledger.activate(tenant_id="tenant-a", job_id=outcomes[0]["job_id"])
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
