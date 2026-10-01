from datetime import UTC, datetime

import pytest

from app.retrieval.retention import (
    NewsRetentionCandidate,
    RetentionCandidatePage,
    RetentionDryRunService,
    NewsRetentionService,
    RetainedChunk,
    RetentionAction,
    RetentionPolicy,
    RetentionReason,
)
from app.retrieval.tiered_vector import VectorTier


class FakeVectorIndex:
    def __init__(self, *, keep_after_delete=False):
        self.keep_after_delete = keep_after_delete
        self.calls = []

    async def delete(self, *, tier, chunk_ids):
        self.calls.append(("vector_delete", tier, tuple(chunk_ids)))
        return len(chunk_ids)

    async def contains(self, *, tier, chunk_ids):
        self.calls.append(("vector_verify", tier, tuple(chunk_ids)))
        return frozenset(chunk_ids) if self.keep_after_delete else frozenset()


class FakeFullText:
    def __init__(self, calls):
        self.calls = calls

    async def delete_news(self, **kwargs):
        self.calls.append(("full_text", kwargs["news_id"]))


class FakeCache:
    def __init__(self, calls):
        self.calls = calls

    async def invalidate_news(self, **kwargs):
        self.calls.append(("cache", kwargs["news_id"]))


class FakeObjects:
    def __init__(self, calls):
        self.calls = calls

    async def archive(self, *, object_uri):
        self.calls.append(("archive", object_uri))

    async def delete(self, *, object_uri):
        self.calls.append(("object_delete", object_uri))


class FakeAudit:
    def __init__(self, calls):
        self.calls = calls

    async def append(self, event):
        self.calls.append(
            ("audit", event.status, event.approved_by, event.error_type)
        )


def candidate(**overrides):
    values = {
        "tenant_id": "tenant-1",
        "news_id": "news-1",
        "content_version": 1,
        "publish_time": datetime(2016, 9, 23, tzinfo=UTC),
        "object_uri": "cos://news/news-1/v1.json",
        "chunks": (
            RetainedChunk("chunk-hot", VectorTier.HOT),
            RetainedChunk("chunk-cold", VectorTier.COLD),
        ),
        "object_size_bytes": 1024,
    }
    values.update(overrides)
    return NewsRetentionCandidate(**values)


def service(vector=None, *, expired_action=RetentionAction.ARCHIVE):
    calls = []
    instance = NewsRetentionService(
        policy=RetentionPolicy(expired_action=expired_action),
        vector_index=vector or FakeVectorIndex(),
        full_text=FakeFullText(calls),
        cache=FakeCache(calls),
        objects=FakeObjects(calls),
        audit=FakeAudit(calls),
    )
    return instance, calls


def test_policy_uses_calendar_year_cutoff_and_legal_hold_wins() -> None:
    policy = RetentionPolicy(retention_years=10)
    now = datetime(2028, 2, 29, 12, tzinfo=UTC)

    expired = policy.decide(
        candidate(publish_time=datetime(2018, 2, 28, 11, tzinfo=UTC)),
        now=now,
    )
    held = policy.decide(
        candidate(legal_hold=True, withdrawn=True),
        now=now,
    )

    assert expired.action is RetentionAction.ARCHIVE
    assert expired.cutoff == datetime(2018, 2, 28, 12, tzinfo=UTC)
    assert held.action is RetentionAction.RETAIN
    assert held.reason is RetentionReason.LEGAL_HOLD


@pytest.mark.asyncio
async def test_dry_run_has_no_side_effects() -> None:
    vector = FakeVectorIndex()
    retention, calls = service(vector)

    result = await retention.execute(
        candidate(),
        now=datetime(2026, 9, 23, tzinfo=UTC),
    )

    assert result.dry_run is True
    assert result.action is RetentionAction.ARCHIVE
    assert vector.calls == []
    assert calls == []


@pytest.mark.asyncio
async def test_execution_requires_explicit_approval() -> None:
    retention, _ = service()
    with pytest.raises(PermissionError, match="approved_by"):
        await retention.execute(
            candidate(),
            now=datetime(2026, 9, 23, tzinfo=UTC),
            dry_run=False,
        )


@pytest.mark.asyncio
async def test_archive_removes_projections_before_archiving_object() -> None:
    vector = FakeVectorIndex()
    retention, calls = service(vector)

    result = await retention.execute(
        candidate(),
        now=datetime(2026, 9, 23, tzinfo=UTC),
        dry_run=False,
        approved_by="operator-1",
    )

    assert result.deleted_vector_chunks == 2
    assert [call[0] for call in vector.calls] == [
        "vector_delete",
        "vector_verify",
        "vector_delete",
        "vector_verify",
    ]
    assert calls == [
        ("audit", "started", "operator-1", None),
        ("full_text", "news-1"),
        ("cache", "news-1"),
        ("archive", "cos://news/news-1/v1.json"),
        ("audit", "completed", "operator-1", None),
    ]


@pytest.mark.asyncio
async def test_vector_residue_prevents_canonical_object_deletion() -> None:
    vector = FakeVectorIndex(keep_after_delete=True)
    retention, calls = service(
        vector,
        expired_action=RetentionAction.DELETE,
    )

    with pytest.raises(RuntimeError, match="verification failed"):
        await retention.execute(
            candidate(),
            now=datetime(2026, 9, 23, tzinfo=UTC),
            dry_run=False,
            approved_by="operator-1",
        )

    assert calls == [
        ("audit", "started", "operator-1", None),
        ("audit", "failed", "operator-1", "RuntimeError"),
    ]
    assert not any(call[0] == "object_delete" for call in calls)


@pytest.mark.asyncio
async def test_withdrawn_news_uses_immediate_delete_path() -> None:
    retention, calls = service()

    result = await retention.execute(
        candidate(
            publish_time=datetime(2026, 9, 22, tzinfo=UTC),
            withdrawn=True,
        ),
        now=datetime(2026, 9, 23, tzinfo=UTC),
        dry_run=False,
        approved_by="compliance-1",
    )

    assert result.action is RetentionAction.DELETE
    assert result.reason is RetentionReason.WITHDRAWN
    assert ("object_delete", "cos://news/news-1/v1.json") in calls


class FakeCandidateRepository:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def list_candidates(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages[kwargs["cursor"]]


@pytest.mark.asyncio
async def test_dry_run_report_scans_pages_and_summarizes_tiers() -> None:
    repository = FakeCandidateRepository(
        {
            None: RetentionCandidatePage(
                items=(candidate(news_id="old-news"),),
                next_cursor="page-2",
            ),
            "page-2": RetentionCandidatePage(
                items=(
                    candidate(
                        news_id="held-news",
                        legal_hold=True,
                        chunks=(RetainedChunk("held-warm", VectorTier.WARM),),
                        object_size_bytes=2048,
                    ),
                    candidate(
                        news_id="withdrawn-news",
                        withdrawn=True,
                        publish_time=datetime(2026, 9, 23, tzinfo=UTC),
                        chunks=(RetainedChunk("withdrawn-hot", VectorTier.HOT),),
                        object_size_bytes=4096,
                    ),
                ),
                next_cursor=None,
            ),
        }
    )
    scanner = RetentionDryRunService(
        repository=repository,
        policy=RetentionPolicy(),
        page_size=2,
    )

    report = await scanner.scan(
        tenant_id="tenant-1",
        now=datetime(2026, 9, 24, tzinfo=UTC),
    )

    assert report.scanned_news == 3
    assert report.archive_news == 1
    assert report.delete_news == 1
    assert report.retain_news == 1
    assert report.legal_hold_news == 1
    assert report.withdrawn_news == 1
    assert report.total_chunks == 4
    assert report.chunks_by_tier == {"hot": 2, "warm": 1, "cold": 1}
    assert report.estimated_object_bytes == 7168
    assert report.sample_news_ids == ("old-news", "withdrawn-news")
    assert report.truncated is False
    assert all(call["published_before"] == report.cutoff for call in repository.calls)


@pytest.mark.asyncio
async def test_dry_run_report_rejects_cross_tenant_candidates() -> None:
    repository = FakeCandidateRepository(
        {
            None: RetentionCandidatePage(
                items=(candidate(tenant_id="tenant-2"),),
                next_cursor=None,
            )
        }
    )
    scanner = RetentionDryRunService(
        repository=repository,
        policy=RetentionPolicy(),
    )

    with pytest.raises(RuntimeError, match="tenant boundary"):
        await scanner.scan(
            tenant_id="tenant-1",
            now=datetime(2026, 9, 24, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_dry_run_report_honors_candidate_limit() -> None:
    repository = FakeCandidateRepository(
        {
            None: RetentionCandidatePage(
                items=(candidate(news_id="news-1"),),
                next_cursor="more",
            )
        }
    )
    scanner = RetentionDryRunService(
        repository=repository,
        policy=RetentionPolicy(),
        page_size=100,
    )

    report = await scanner.scan(
        tenant_id="tenant-1",
        now=datetime(2026, 9, 24, tzinfo=UTC),
        max_candidates=1,
    )

    assert report.scanned_news == 1
    assert report.truncated is True
