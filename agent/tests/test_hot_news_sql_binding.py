"""Stable scope and strict frozen authorization for the hot-news SQL tool."""

from datetime import timedelta, timezone
from uuid import uuid4
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager

import pytest

from app.services.hot_news_orchestration import HotNewsRunRequest
from app.sql_assistant.hot_news_binding import HotNewsSqlBindingStore, HotNewsSqlBindingError, build_hot_news_sql_scope
from app.sql_assistant.warehouse import SCHEMA_SHA256, WINDOW_START

TENANT = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"


def scope(**changes):
    return build_hot_news_sql_scope(**{
        "question": "点击前5条新闻", "scenario_id": "news-ranking", "window_start": WINDOW_START,
        "window_end": WINDOW_START + timedelta(hours=1), "schema_sha256": SCHEMA_SHA256,
        "scenario": {"sort": "clicks"}, "production_bundle_version": "bundle-v1",
        "model_scene": "text2sql_assistant", "user_id": USER, **changes,
    })


def request(query_id=None, digest=None):
    return HotNewsRunRequest(
        tenant_id=TENANT, window_start=WINDOW_START, window_end=WINDOW_START + timedelta(hours=1),
        production_bundle_version="bundle-v1", sql_query_id=query_id or str(uuid4()),
        sql_query_user_id=USER, sql_query_scope_sha256=digest or scope(),
    )


def binding():
    run = request()
    return {"run_key": run.idempotency_key, "query_id": run.sql_query_id,
            "tenant_id": TENANT, "user_id": USER, "production_bundle_version": "bundle-v1",
            "scope_sha256": run.sql_query_scope_sha256, "window_start": run.window_start, "window_end": run.window_end}


def test_scope_and_run_identity_are_stable_across_preview_ids_and_timezones():
    assert scope() == scope(window_start=WINDOW_START.astimezone(timezone.utc),
                            window_end=(WINDOW_START + timedelta(hours=1)).astimezone(timezone.utc))
    assert request().idempotency_key == request().idempotency_key
    legacy = HotNewsRunRequest(TENANT, WINDOW_START, WINDOW_START + timedelta(hours=1), "bundle-v1")
    assert legacy.idempotency_key != request().idempotency_key


@pytest.mark.parametrize("changes", [{"question": "点击前3条新闻"}, {"scenario": {"sort": "ctr"}},
    {"production_bundle_version": "bundle-v2"}, {"user_id": str(uuid4())}])
def test_semantic_and_owner_changes_create_a_different_run(changes):
    assert scope(**changes) != scope()
    assert request(digest=scope(**changes)).idempotency_key != request().idempotency_key


def test_partial_sql_reference_is_rejected():
    with pytest.raises(ValueError, match="include query"):
        HotNewsRunRequest(TENANT, WINDOW_START, WINDOW_START + timedelta(hours=1), "bundle-v1", sql_query_id=str(uuid4())).validate()


def test_canonical_claim_only_allows_different_query_uuid():
    expected = binding()
    row = {**expected, "query_id": str(uuid4())}
    assert HotNewsSqlBindingStore._validate_binding(row, expected, canonical_query=True) == row
    with pytest.raises(HotNewsSqlBindingError):
        HotNewsSqlBindingStore._validate_binding(row, expected, canonical_query=False)


@pytest.mark.parametrize("field,value", [("tenant_id", str(uuid4())), ("user_id", str(uuid4())),
    ("production_bundle_version", "bundle-v2"), ("scope_sha256", "b"*64), ("window_end", WINDOW_START + timedelta(hours=2))])
def test_claim_cannot_change_frozen_authorization(field, value):
    expected = binding()
    with pytest.raises(HotNewsSqlBindingError):
        HotNewsSqlBindingStore._validate_binding({**expected, field: value}, expected, canonical_query=True)


@pytest.mark.asyncio
async def test_require_is_tenant_and_owner_scoped_and_missing_claim_is_terminal():
    result = AsyncMock()
    result.mappings = lambda: type("Rows", (), {"one_or_none": lambda self: None})()
    session = AsyncMock()
    session.execute.return_value = result
    class Database:
        @asynccontextmanager
        async def session(self):
            yield session
    with pytest.raises(HotNewsSqlBindingError) as error:
        await HotNewsSqlBindingStore(Database()).require(**binding())
    assert error.value.retryable is False
    statement, params = session.execute.call_args.args
    assert "tenant_id=CAST(:tenant_id" in str(statement)
    assert params["tenant_id"] == TENANT and params["user_id"] == USER
