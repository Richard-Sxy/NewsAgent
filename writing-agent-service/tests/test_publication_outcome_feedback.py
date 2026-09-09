from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.schemas.analysis_feedback import (
    PublicationOutcome,
    PublicationOutcomeMetrics,
    RecordPublicationOutcomeCommand,
)
from app.services.data_loop.feedback_collector import (
    AnalysisFeedbackTargetNotFoundError,
    FeedbackCaseWriteOutcome,
    PublicationOutcomeWriteOutcome,
)
from app.services.data_loop.publication_outcome import (
    PublicationOutcomeFeedbackPolicy,
    PublicationOutcomeFeedbackService,
)


NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
OUTCOME_ID = UUID("22222222-2222-4222-8222-222222222222")


def command(**metric_overrides) -> RecordPublicationOutcomeCommand:
    metrics = {
        "impressions": 1000,
        "clicks": 5,
        "unique_users": 900,
        "effective_consumptions": 3,
        "interactions": 1,
        "complaints": 0,
        "corrections": 0,
    }
    metrics.update(metric_overrides)
    return RecordPublicationOutcomeCommand(
        run_id=RUN_ID,
        run_idempotency_key="hot-news-run-1",
        news_id="news-1",
        external_publication_id="cms-1",
        source_system="cms",
        metric_definition_version="metrics-v1",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        metrics=PublicationOutcomeMetrics(**metrics),
        idempotency_key="publication-window-1",
    )


def outcome(request: RecordPublicationOutcomeCommand) -> PublicationOutcome:
    return PublicationOutcome(
        id=OUTCOME_ID,
        tenant_id="tenant-1",
        recorded_at=NOW,
        content_sha256="a" * 64,
        **request.model_dump(),
    )


@pytest.mark.asyncio
async def test_low_ctr_creates_feedback_from_aggregate_outcome() -> None:
    request = command()
    stored = outcome(request)
    memory = SimpleNamespace(
        run_id=RUN_ID,
        news_id="news-1",
        run_idempotency_key="hot-news-run-1",
    )
    collector = SimpleNamespace(
        record_publication_outcome=AsyncMock(
            return_value=PublicationOutcomeWriteOutcome(stored, True)
        ),
        collect_from_publication_outcome=AsyncMock(
            return_value=FeedbackCaseWriteOutcome(
                case=SimpleNamespace(id="case-1"), created=True
            )
        ),
    )
    service = PublicationOutcomeFeedbackService(
        memory_store=SimpleNamespace(
            get_analysis_memory=AsyncMock(return_value=memory)
        ),
        collector=collector,
    )

    result = await service.record_and_collect(
        object(),
        tenant_id="tenant-1",
        command=request,
        recorded_at=NOW,
    )

    assert result.outcome_created is True
    assert result.feedback_case is not None
    call = collector.collect_from_publication_outcome.await_args
    assert call.kwargs["severity"] == "medium"
    assert "publication-outcome-feedback-v1" in call.kwargs["idempotency_key"]


def test_publication_policy_uses_only_aggregate_counts() -> None:
    policy = PublicationOutcomeFeedbackPolicy()

    assert policy.classify(command(clicks=30)) is None
    assert policy.classify(command(clicks=30, corrections=1)) == "high"
    assert policy.classify(command(clicks=30, complaints=60)) == "critical"


@pytest.mark.asyncio
async def test_publication_outcome_rejects_wrong_run_key() -> None:
    memory_store = SimpleNamespace(
        get_analysis_memory=AsyncMock(
            return_value=SimpleNamespace(
                run_idempotency_key="different-run",
            )
        )
    )
    collector = SimpleNamespace(record_publication_outcome=AsyncMock())
    service = PublicationOutcomeFeedbackService(
        memory_store=memory_store,
        collector=collector,
    )

    with pytest.raises(AnalysisFeedbackTargetNotFoundError):
        await service.record_and_collect(
            object(),
            tenant_id="tenant-1",
            command=command(),
            recorded_at=NOW,
        )

    collector.record_publication_outcome.assert_not_awaited()
