"""Opt-in real PostgreSQL checks; use a new disposable database only."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.sql_assistant.audit import PostgresQuerySnapshotStore, initialize_query_audit
from app.sql_assistant.hot_news_binding import (
    HotNewsSqlBindingError, HotNewsSqlBindingPersistenceError, HotNewsSqlBindingStore,
)


class IsolatedDatabase:
    def __init__(self, url):
        self.engine = create_async_engine(url, pool_size=5, max_overflow=0)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self):
        async with self.factory() as session:
            async with session.begin():
                yield session


def expected():
    return {
        "run_key": "candidate-test-" + str(uuid4()), "query_id": str(uuid4()),
        "tenant_id": str(uuid4()), "user_id": str(uuid4()),
        "production_bundle_version": "test-bundle", "scope_sha256": "a" * 64,
        "window_start": datetime(2026, 10, 3, 2, tzinfo=timezone.utc),
        "window_end": datetime(2026, 10, 3, 3, tzinfo=timezone.utc),
    }


async def save_preview(database, identity):
    query_id = str(uuid4())
    await PostgresQuerySnapshotStore(database).save({
        "tenant_id": identity["tenant_id"], "user_id": identity["user_id"],
        "preview": {"query_id": query_id,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()},
    })
    return SimpleNamespace(query_id=query_id)


async def database():
    url = os.environ.get("SQL_CANDIDATE_BINDING_POSTGRES_URL")
    if not url:
        pytest.skip("requires an explicitly isolated SQL_CANDIDATE_BINDING_POSTGRES_URL")
    db = IsolatedDatabase(url)
    await initialize_query_audit(db, environment="e2e")
    await HotNewsSqlBindingStore(db).initialize(environment="e2e")
    return db


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_parallel_generation_is_single_and_restart_reuses_claim():
    db = await database()
    task = None
    try:
        identity = expected()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def build():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return await save_preview(db, identity)

        store = HotNewsSqlBindingStore(db)
        task = asyncio.create_task(store.plan_once(build_preview=build, **identity))
        await asyncio.wait_for(entered.wait(), timeout=3)
        started = monotonic()
        with pytest.raises(HotNewsSqlBindingPersistenceError, match="in progress"):
            await asyncio.wait_for(HotNewsSqlBindingStore(db).plan_once(
                build_preview=build, **{**identity, "query_id": str(uuid4())},
            ), timeout=2)
        assert monotonic() - started < 1.0
        assert calls == 1
        release.set()
        saved = await asyncio.wait_for(task, timeout=3)
        restored = await HotNewsSqlBindingStore(db).plan_once(
            build_preview=build, **{**identity, "query_id": str(uuid4())},
        )
        assert saved == restored and calls == 1
        assert await store.require(**saved) == saved
        async with db.session() as session:
            count = await session.execute(text(
                "SELECT count(*) FROM sql_assistant_query_audit WHERE tenant_id=CAST(:tenant_id AS uuid)"
            ), {"tenant_id": identity["tenant_id"]})
            assert count.scalar_one() == 1
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await db.engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_cancel_releases_lock_and_orphan_cannot_be_used_as_run():
    db = await database()
    task = None
    try:
        identity = expected()
        entered = asyncio.Event()
        orphan = None

        async def interrupted():
            nonlocal orphan
            orphan = await save_preview(db, identity)
            entered.set()
            await asyncio.Event().wait()

        store = HotNewsSqlBindingStore(db)
        task = asyncio.create_task(store.plan_once(build_preview=interrupted, **identity))
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(HotNewsSqlBindingError, match="no matching"):
            await store.require(**{**identity, "query_id": orphan.query_id})

        async def recovered():
            return await save_preview(db, identity)

        saved = await asyncio.wait_for(store.plan_once(build_preview=recovered, **identity), timeout=3)
        assert saved["query_id"] != orphan.query_id
        assert await store.require(**saved) == saved
        with pytest.raises(HotNewsSqlBindingError, match="frozen authorization"):
            await store.require(**{**identity, "query_id": orphan.query_id})
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await db.engine.dispose()
