"""Candidate planning reuses a claimed run and never waits on a model lock."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.sql_assistant.hot_news_binding import (
    HotNewsSqlBindingError, HotNewsSqlBindingPersistenceError,
    HotNewsSqlBindingStore,
)
from tests.test_hot_news_sql_binding import binding, scope


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def mappings(self):
        return self

    def one_or_none(self):
        return self.value


class Database:
    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.active = []
        self.exits = []

    @asynccontextmanager
    async def session(self):
        session = self.sessions.pop(0)
        self.active.append(session)
        outcome = "committed"
        try:
            yield session
        except BaseException:
            outcome = "rolled_back"
            raise
        finally:
            self.active.remove(session)
            self.exits.append((session, outcome))


def session(*results):
    return SimpleNamespace(execute=AsyncMock(side_effect=[Result(item) for item in results]))


@pytest.mark.asyncio
async def test_existing_claim_is_reused_without_generating_another_preview():
    saved = binding()
    locking = session(True, saved)
    database = Database(locking)
    build = AsyncMock()
    result = await HotNewsSqlBindingStore(database).plan_once(
        build_preview=build, **{**saved, "query_id": str(uuid4())},
    )
    assert result["query_id"] == saved["query_id"]
    build.assert_not_awaited()
    assert not database.active
    assert database.exits == [(locking, "committed")]
    statement, params = locking.execute.call_args_list[0].args
    assert "pg_try_advisory_xact_lock" in str(statement)
    expected_key = int.from_bytes(hashlib.sha256(saved["run_key"].encode()).digest()[:8], "big", signed=True)
    assert params == {"lock_key": expected_key}
    assert -(2**63) <= expected_key < 2**63


@pytest.mark.asyncio
async def test_busy_run_fails_immediately_without_generation_or_binding_lookup():
    locking = session(False)
    database = Database(locking)
    build = AsyncMock()
    with pytest.raises(HotNewsSqlBindingPersistenceError) as error:
        await HotNewsSqlBindingStore(database).plan_once(build_preview=build, **binding())
    assert error.value.retryable is True and error.value.status_code == 503
    build.assert_not_awaited()
    assert locking.execute.await_count == 1
    assert database.exits == [(locking, "rolled_back")]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("tenant_id", str(uuid4())), ("user_id", str(uuid4())),
    ("scope_sha256", "f" * 64), ("production_bundle_version", "changed-bundle"),
])
async def test_existing_claim_cannot_change_its_frozen_authorization(field, value):
    saved = binding()
    database = Database(session(True, {**saved, field: value}))
    build = AsyncMock()
    with pytest.raises(HotNewsSqlBindingError, match="frozen authorization"):
        await HotNewsSqlBindingStore(database).plan_once(build_preview=build, **saved)
    build.assert_not_awaited()
    assert not database.active


@pytest.mark.asyncio
async def test_preview_is_built_and_claim_committed_before_releasing_lock():
    expected = binding()
    preview_id = str(uuid4())
    saved = {**expected, "query_id": preview_id}
    locking = session(True, None)
    claiming = session(saved)
    database = Database(locking, claiming)

    async def build_preview():
        assert database.active == [locking]
        return SimpleNamespace(query_id=preview_id)

    result = await HotNewsSqlBindingStore(database).plan_once(
        build_preview=build_preview, **expected,
    )
    assert result["query_id"] == preview_id
    assert database.exits == [(claiming, "committed"), (locking, "committed")]
    statement, params = claiming.execute.call_args.args
    assert "INSERT INTO public.hot_news_sql_bindings" in str(statement)
    assert "expires_at > CURRENT_TIMESTAMP" in str(statement)
    assert params["query_id"] == preview_id
    assert params["tenant_id"] == expected["tenant_id"]
    assert params["user_id"] == expected["user_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("generation failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_generation_releases_lock_without_claiming(failure):
    locking = session(True, None)
    database = Database(locking)
    build = AsyncMock(side_effect=failure)
    with pytest.raises(type(failure)):
        await HotNewsSqlBindingStore(database).plan_once(build_preview=build, **binding())
    assert locking.execute.await_count == 2
    assert not database.active
    assert database.exits == [(locking, "rolled_back")]


@pytest.mark.asyncio
async def test_database_failure_is_retryable_and_does_not_generate():
    locking = SimpleNamespace(execute=AsyncMock(side_effect=SQLAlchemyError("unavailable")))
    database = Database(locking)
    build = AsyncMock()
    with pytest.raises(HotNewsSqlBindingPersistenceError) as error:
        await HotNewsSqlBindingStore(database).plan_once(build_preview=build, **binding())
    assert error.value.retryable
    build.assert_not_awaited()
    assert not database.active


@pytest.mark.asyncio
async def test_failed_claim_releases_generation_lock_and_returns_no_binding():
    locking = session(True, None)
    claiming = session(None, None, None)
    database = Database(locking, claiming)
    build = AsyncMock(return_value=SimpleNamespace(query_id=str(uuid4())))
    with pytest.raises(HotNewsSqlBindingError, match="unavailable"):
        await HotNewsSqlBindingStore(database).plan_once(build_preview=build, **binding())
    assert database.exits == [(claiming, "rolled_back"), (locking, "rolled_back")]
    assert not database.active


def test_generation_identity_changes_scope_and_native_none_preserves_legacy_scope():
    native = scope()
    assert native == scope(generation_identity=None)
    identity = {"mode": "dbgpt_candidate", "adapter_version": "v1", "prompt_sha256": "a" * 64}
    generated = scope(generation_identity=identity)
    assert generated != native
    assert generated == scope(generation_identity=dict(reversed(list(identity.items()))))
    for field, value in (("mode", "dbgpt_shadow"), ("adapter_version", "v2"), ("prompt_sha256", "b" * 64)):
        assert scope(generation_identity={**identity, field: value}) != generated


@pytest.mark.parametrize("identity", [{}, [], "v1", {1: "x"}, {"": "x"}, {"a": float("nan")}, {"a": object()}])
def test_generation_identity_requires_nonempty_finite_json(identity):
    with pytest.raises(HotNewsSqlBindingError):
        scope(generation_identity=identity)
