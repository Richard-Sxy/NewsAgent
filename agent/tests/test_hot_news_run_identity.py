from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.domain.errors import HotNewsDataQualityError
from app.services.hot_news_run_store import PostgresHotNewsRunStore


START = datetime(2026, 9, 10, 8, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def _run(**changes):
    values = {
        "tenant_id": "tenant-1",
        "idempotency_key": "hot-news-key",
        "window_start": START,
        "window_end": END,
        "production_bundle_version": "bundle-v1",
        "workflow_version": "workflow-v1",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _validate(run) -> None:
    PostgresHotNewsRunStore._validate_run_identity(
        run=run,
        tenant_id="tenant-1",
        idempotency_key="hot-news-key",
        window_start=START,
        window_end=END,
        production_bundle_version="bundle-v1",
        workflow_version="workflow-v1",
    )


def test_exact_run_identity_is_replayable() -> None:
    _validate(_run())


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tenant_id", "tenant-2"),
        ("idempotency_key", "other-key"),
        ("window_start", START - timedelta(hours=1)),
        ("window_end", END + timedelta(hours=1)),
        ("production_bundle_version", "bundle-v2"),
        ("workflow_version", "workflow-v2"),
    ),
)
def test_any_idempotency_alias_is_rejected(field: str, value: object) -> None:
    with pytest.raises(HotNewsDataQualityError, match="different run content"):
        _validate(_run(**{field: value}))
