"""Exercise actual executor threads: cancellation must not free SDK capacity."""

import asyncio
from datetime import UTC, datetime
from threading import Event, Lock
import sys
from types import SimpleNamespace

import pytest

from app.clients.enterprise.milvus_vector import MilvusTieredVectorIndex
from app.retrieval.tiered_vector import ChunkVectorRecord, VectorTier


class BlockingClient:
    def __init__(self):
        self.release = Event()
        self.lock = Lock()
        self.started = self.active = self.peak = self.closed = 0
        self.failure = None
        self.timeouts = []

    def _call(self, value, **kwargs):
        with self.lock:
            self.started += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.timeouts.append(kwargs["timeout"])
        try:
            if not self.release.wait(timeout=kwargs["timeout"]):
                raise TimeoutError("simulated SDK timeout")
            if self.failure:
                raise self.failure
            return value
        finally:
            with self.lock:
                self.active -= 1

    def search(self, **kwargs):
        return self._call([[]], **kwargs)

    def query(self, **kwargs):
        return self._call([], **kwargs)

    def upsert(self, **kwargs):
        return self._call({"upsert_count": len(kwargs["data"])}, **kwargs)

    def delete(self, **kwargs):
        return self._call({"delete_count": 1}, **kwargs)

    def close(self):
        assert self.active == 0
        self.closed += 1


def build(client, *, capacity=1, timeout=2.0):
    return MilvusTieredVectorIndex(client, collections={VectorTier.HOT: "hot"},
                                  rpc_max_concurrency=capacity, timeout_seconds=timeout)


async def search(index):
    return await index.search(tier=VectorTier.HOT, tenant_id="tenant", vector=(1.0, 0.0),
        embedding_version="model-v1", exclude_news_ids=frozenset(), limit=1)


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.001)


async def cancel(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancelled_call_retains_capacity_and_waiting_call_can_be_cancelled():
    client = BlockingClient()
    index = build(client)
    try:
        first = asyncio.create_task(search(index))
        await until(lambda: client.started == 1)
        await cancel(first)
        waiting = asyncio.create_task(search(index))
        await asyncio.sleep(.01)
        assert client.active == client.started == 1
        assert len(index._rpc_futures) == 1
        await cancel(waiting)
        assert client.started == 1
        third = asyncio.create_task(search(index))
        client.release.set()
        assert await third == []
        await until(lambda: not index._rpc_futures)
        assert client.started == 2 and client.peak == 1
        assert client.timeouts == [2.0, 2.0]
    finally:
        client.release.set()
        await index.close()


@pytest.mark.asyncio
async def test_repeated_timeout_waves_never_queue_more_than_rpc_capacity():
    client = BlockingClient()
    index = build(client, capacity=2)
    try:
        async def timeout_search():
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(.03):
                    await search(index)

        await asyncio.gather(*(timeout_search() for _ in range(12)))
        assert client.started == client.active == 2
        assert len(index._rpc_futures) == 2
        await asyncio.gather(*(timeout_search() for _ in range(12)))
        assert client.started == client.active == client.peak == 2
        assert len(index._rpc_futures) == 2
        client.release.set()
        await until(lambda: not index._rpc_futures)
        assert await search(index) == []
        assert client.started == 3
    finally:
        client.release.set()
        await index.close()


@pytest.mark.asyncio
async def test_detached_sdk_failure_is_consumed_and_capacity_recovers():
    client = BlockingClient()
    client.failure = RuntimeError("private provider error")
    index = build(client)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _, context: unhandled.append(context))
    try:
        task = asyncio.create_task(search(index))
        await until(lambda: client.started == 1)
        await cancel(task)
        client.release.set()
        await until(lambda: not index._rpc_futures)
        client.failure = None
        assert await search(index) == []
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous_handler)
        client.release.set()
        await index.close()


@pytest.mark.asyncio
async def test_close_waits_for_detached_rpc_rejects_new_work_and_closes_once():
    client = BlockingClient()
    index = build(client)
    try:
        task = asyncio.create_task(search(index))
        await until(lambda: client.started == 1)
        waiting = asyncio.create_task(search(index))
        await asyncio.sleep(0)
        await cancel(task)
        closing = asyncio.create_task(index.close())
        await until(lambda: index._closing)
        assert client.closed == 0 and not closing.done()
        with pytest.raises(RuntimeError, match="closing"):
            await search(index)
        await cancel(closing)
        client.release.set()
        with pytest.raises(RuntimeError, match="closing"):
            await waiting
        await asyncio.gather(index.close(), index.close())
        assert client.closed == 1 and client.active == 0
        assert not index._rpc_futures
    finally:
        client.release.set()
        await index.close()


@pytest.mark.asyncio
async def test_sdk_timeout_releases_slot_without_blocking_event_loop():
    client = BlockingClient()
    index = build(client, timeout=.03)
    try:
        task = asyncio.create_task(search(index))
        await until(lambda: client.started == 1)
        assert client.active == 1 and not task.done()
        with pytest.raises(TimeoutError):
            await task
        client.release.set()
        assert await search(index) == []
        assert client.timeouts == [.03, .03]
    finally:
        client.release.set()
        await index.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["upsert", "contains", "verify_records", "delete"])
async def test_write_and_verification_calls_share_search_rpc_budget(operation):
    client = BlockingClient()
    index = build(client)
    record = ChunkVectorRecord(chunk_id="id", tenant_id="tenant", news_id="news", content_version=1,
        chunk_index=0, embedding_version="model-v1", title="title", excerpt="body", source_url=None,
        publish_time=datetime(2026, 10, 16, tzinfo=UTC), vector=(1.0, 0.0))
    try:
        first = asyncio.create_task(search(index))
        await until(lambda: client.started == 1)
        await cancel(first)
        kwargs = {"tier": VectorTier.HOT}
        kwargs["records" if operation in ("upsert", "verify_records") else "chunk_ids"] = (
            [record] if operation in ("upsert", "verify_records") else ["id"])
        second = asyncio.create_task(getattr(index, operation)(**kwargs))
        await asyncio.sleep(.01)
        assert client.started == 1
        client.release.set()
        await second
        assert client.started == 2 and client.peak == 1
    finally:
        client.release.set()
        await index.close()


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5, 769])
def test_rpc_budget_requires_bounded_integer(capacity):
    with pytest.raises(ValueError, match="rpc_max_concurrency"):
        build(BlockingClient(), capacity=capacity)


@pytest.mark.asyncio
async def test_executor_submission_failure_returns_capacity(monkeypatch):
    client = BlockingClient()
    client.release.set()
    index = build(client)
    loop = asyncio.get_running_loop()
    original = loop.run_in_executor

    def cannot_submit(*args, **kwargs):
        raise RuntimeError("executor unavailable")

    try:
        monkeypatch.setattr(loop, "run_in_executor", cannot_submit)
        with pytest.raises(RuntimeError, match="executor unavailable"):
            await search(index)
        monkeypatch.setattr(loop, "run_in_executor", original)
        assert await asyncio.wait_for(search(index), timeout=1) == []
        assert client.started == 1
    finally:
        monkeypatch.setattr(loop, "run_in_executor", original)
        await index.close()


def test_connect_rejects_invalid_budget_before_constructing_sdk(monkeypatch):
    def unexpected_connection(**kwargs):
        pytest.fail("invalid capacity must not create an SDK connection")
    monkeypatch.setitem(sys.modules, "pymilvus", SimpleNamespace(MilvusClient=unexpected_connection))
    with pytest.raises(ValueError, match="rpc_max_concurrency"):
        MilvusTieredVectorIndex.connect(uri="http://localhost:19530", token=None,
            database="default", collections={VectorTier.HOT: "hot"}, rpc_max_concurrency=0)


@pytest.mark.asyncio
async def test_preflight_uses_the_same_rpc_budget_as_detached_search():
    from tests.test_milvus_lifecycle_preflight import schema

    class SchemaClient(BlockingClient):
        def describe_collection(self, **kwargs):
            return self._call(schema(), **kwargs)

        def list_indexes(self, **kwargs):
            return self._call(["vector_idx"], **kwargs)

        def describe_index(self, **kwargs):
            return self._call({"field_name": "vector", "metric_type": "IP",
                "state": "Finished", "index_type": "HNSW"}, **kwargs)

        def get_load_state(self, **kwargs):
            return self._call({"state": "Loaded"}, **kwargs)

    client = SchemaClient()
    index = build(client)
    try:
        first = asyncio.create_task(search(index))
        await until(lambda: client.started == 1)
        await cancel(first)
        preflight = asyncio.create_task(index.validate_collections(
            expected_dimensions=2, query_max_candidates=16))
        await asyncio.sleep(.01)
        assert client.started == 1
        client.release.set()
        result = await preflight
        assert result["hot"]["status"] == "ready"
        assert client.started == 5 and client.peak == 1
    finally:
        client.release.set()
        await index.close()
