from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.knowledge.tier_lifecycle_index import GuardedTieredIndex
from app.retrieval.tiered_vector import ChunkVectorRecord, VectorSearchHit, VectorTier


def hit(chunk_id: str, *, tier: VectorTier = VectorTier.HOT) -> VectorSearchHit:
    return VectorSearchHit(
        chunk_id=chunk_id,
        news_id="news-1",
        title="新闻",
        excerpt="正文",
        source_url=None,
        publish_time=datetime(2026, 10, 15, tzinfo=UTC),
        raw_score=0.9,
        tier=tier,
        content_version=1,
        embedding_version="embedding-v1",
    )


class Backend:
    tiers = (VectorTier.HOT, VectorTier.WARM, VectorTier.COLD)

    def __init__(self, rows):
        self.rows = rows
        self.limits = []
        self.closed = False

    async def search(self, **kwargs):
        self.limits.append(kwargs["limit"])
        return self.rows[:kwargs["limit"]]

    async def close(self):
        self.closed = True


class Ledger:
    def __init__(self, approved):
        self.approved = set(approved)
        self.calls = []

    async def filter_hits(self, *, tenant_id, hits):
        self.calls.append((tenant_id, list(hits)))
        return [row for row in hits if row.chunk_id in self.approved]


async def search(index, *, limit=2):
    return await index.search(
        tier=VectorTier.HOT,
        tenant_id="tenant-1",
        vector=(1.0, 0.0),
        embedding_version="embedding-v1",
        exclude_news_ids=frozenset(),
        limit=limit,
    )


@pytest.mark.asyncio
async def test_stale_candidates_refill_before_fusion_preserving_rank():
    rows = [hit("old"), hit("withdrawn"), hit("active-1"), hit("active-2")]
    backend = Backend(rows)
    ledger = Ledger({"active-1", "active-2"})
    guarded = GuardedTieredIndex(backend, ledger, max_candidates=6)
    assert await search(guarded) == rows[2:]
    assert backend.limits == [2, 4]
    assert ledger.calls == [("tenant-1", rows[:2]), ("tenant-1", rows)]
    assert guarded.tiers == backend.tiers
    assert guarded.lifecycle_guarded is True


@pytest.mark.asyncio
async def test_refill_rechecks_earlier_approvals_after_concurrent_withdrawal():
    rows = [hit("withdraw-during-refill"), hit("old"), hit("active")]
    backend = Backend(rows)

    class ChangingLedger(Ledger):
        async def filter_hits(self, *, tenant_id, hits):
            if self.calls:
                self.approved = {"active"}
            return await super().filter_hits(tenant_id=tenant_id, hits=hits)

    guarded = GuardedTieredIndex(backend, ChangingLedger({rows[0].chunk_id}))
    assert await search(guarded) == [rows[2]]
    assert backend.limits == [2, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "max_candidates,rounds,expected_limits",
    [(5, 4, [2, 4, 5]), (20, 1, [2, 4]), (20, 0, [2])],
)
async def test_refill_is_bounded_even_when_every_candidate_is_stale(
    max_candidates, rounds, expected_limits
):
    backend = Backend([hit(f"stale-{i}") for i in range(30)])
    guarded = GuardedTieredIndex(
        backend, Ledger(set()), max_candidates=max_candidates, max_refill_rounds=rounds
    )
    assert await search(guarded) == []
    assert backend.limits == expected_limits


@pytest.mark.asyncio
async def test_database_and_index_failures_do_not_return_unguarded_hits():
    class BrokenLedger:
        async def filter_hits(self, **kwargs):
            raise RuntimeError("ledger unavailable")

    guarded = GuardedTieredIndex(Backend([hit("stale")]), BrokenLedger())
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await search(guarded)

    class BrokenBackend(Backend):
        async def search(self, **kwargs):
            raise TimeoutError("vector timeout")

    ledger = Ledger({"stale"})
    guarded = GuardedTieredIndex(BrokenBackend([]), ledger)
    with pytest.raises(TimeoutError, match="vector timeout"):
        await search(guarded)
    assert ledger.calls == []


@pytest.mark.asyncio
async def test_gate_cannot_inject_other_versions_and_does_not_change_vector_rank():
    rows = [hit("a"), hit("b")]

    class OddLedger:
        async def filter_hits(self, **kwargs):
            return [replace(rows[0], content_version=2), rows[1], hit("injected")]

    guarded = GuardedTieredIndex(Backend(rows), OddLedger(), max_refill_rounds=0)
    assert await search(guarded) == [rows[1]]


@pytest.mark.asyncio
async def test_close_and_optional_embedder_forwarding():
    backend = Backend([])
    guarded = GuardedTieredIndex(backend, Ledger(set()))
    assert not hasattr(guarded, "query_embedder")
    await guarded.close()
    assert backend.closed
    backend.query_embedder = object()
    guarded = GuardedTieredIndex(backend, Ledger(set()))
    assert guarded.query_embedder is backend.query_embedder


@pytest.mark.asyncio
async def test_mutation_forwarding_requires_full_record_verification():
    record = ChunkVectorRecord(
        chunk_id="projection-1-chunk-0", tenant_id="tenant-1", news_id="news-1",
        content_version=1, chunk_index=0, embedding_version="embedding-v1",
        title="新闻", excerpt="正文", source_url=None,
        publish_time=datetime(2026, 10, 15, tzinfo=UTC), vector=(1.0, 0.0),
    )

    class MutableBackend(Backend):
        async def upsert(self, *, tier, records):
            assert tier == VectorTier.COLD and records == [record]
            return 1

        async def contains(self, *, tier, chunk_ids):
            return frozenset(chunk_ids)

        async def verify_records(self, *, tier, records):
            return records == [record]

        async def delete(self, *, tier, chunk_ids):
            return len(chunk_ids)

    guarded = GuardedTieredIndex(MutableBackend([]), Ledger(set()))
    assert await guarded.upsert(tier=VectorTier.COLD, records=[record]) == 1
    assert await guarded.contains(tier=VectorTier.COLD, chunk_ids=[record.chunk_id]) == {record.chunk_id}
    assert await guarded.verify_records(tier=VectorTier.COLD, records=[record])
    assert await guarded.delete(tier=VectorTier.COLD, chunk_ids=[record.chunk_id]) == 1
    guarded = GuardedTieredIndex(Backend([]), Ledger(set()))
    with pytest.raises(RuntimeError, match="full record verification"):
        await guarded.verify_records(tier=VectorTier.COLD, records=[record])


@pytest.mark.asyncio
async def test_rejects_unbounded_limits_before_remote_work():
    backend = Backend([])
    guarded = GuardedTieredIndex(backend, Ledger(set()), max_candidates=2)
    with pytest.raises(ValueError, match="max_candidates"):
        await search(guarded, limit=3)
    assert backend.limits == []


@pytest.mark.parametrize(
    "kwargs", [{"max_candidates": 0}, {"refill_factor": 1}, {"max_refill_rounds": -1}]
)
def test_invalid_refill_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        GuardedTieredIndex(Backend([]), Ledger(set()), **kwargs)
