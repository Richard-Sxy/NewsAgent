"""Per-worker receipt reuse remains authorized, tenant-scoped and bounded."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
from temporalio.service import RPCError, RPCStatusCode

from app.modules.conversation.receipt_reads import ReceiptReadCoordinator, ReceiptReadUnavailable
from app.modules.conversation.tools import ConversationToolDenied, HOOK_REGISTRY
from tests.test_conversation_capability_integration import receipt_client
from tests.test_conversation_tools_writing import tools_with_reader, JOB_ID, TENANT


class Tools:
    def __init__(self):
        self.calls = []
        self.checks = 0
        self.enabled = True
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.failure = None
        self.result = None
        self.cancelled = False

    def hook_spec(self, name):
        return HOOK_REGISTRY.get(name)

    def _hook_enabled(self, spec):
        self.checks += 1
        return self.enabled

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        try:
            await self.release.wait()
            if self.failure:
                raise self.failure
            if self.result is not None:
                return self.result
            return {"job": {"id": kwargs["arguments"].get("job_id")},
                    "source_tenant": kwargs["tenant_id"], "version": len(self.calls)}
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def read(reader, tools, *, tenant=TENANT, user="reader", job=JOB_ID, permissions=None):
    return await reader.read(tools=tools, tenant_id=tenant, user_id=user,
        name="writing_job_status", arguments={"job_id": job},
        permissions=frozenset({"writing:read"}) if permissions is None else permissions)


@pytest.mark.asyncio
async def test_concurrent_users_share_one_read_but_each_rechecks_hook_and_permission():
    tools, reader = Tools(), ReceiptReadCoordinator()
    tools.release.clear()
    tasks = [asyncio.create_task(read(reader, tools, user=f"reader-{index}")) for index in range(30)]
    await tools.started.wait()
    await asyncio.sleep(0)
    assert len(tools.calls) == 1 and tools.checks == 30
    with pytest.raises(ConversationToolDenied, match="permission_denied"):
        await read(reader, tools, permissions=frozenset({"memory:admin"}))
    tools.release.set()
    results = await asyncio.gather(*tasks)
    assert all(result == results[0] for result in results)
    results[0]["job"]["id"] = "mutated-client-copy"
    assert results[1]["job"]["id"] == JOB_ID
    assert (await read(reader, tools))["job"]["id"] == JOB_ID
    assert len(tools.calls) == 1
    with pytest.raises(ConversationToolDenied, match="permission_denied"):
        await read(reader, tools, permissions=frozenset())
    tools.enabled = False
    with pytest.raises(ConversationToolDenied, match="not_enabled"):
        await read(reader, tools)
    assert len(tools.calls) == 1
    await reader.close()


@pytest.mark.asyncio
async def test_tenant_entity_and_hook_keys_do_not_reuse_different_projection():
    tools, reader = Tools(), ReceiptReadCoordinator()
    first = await read(reader, tools)
    second = await read(reader, tools, tenant=str(uuid4()))
    third = await read(reader, tools, job=str(uuid4()))
    evaluation = await reader.read(tools=tools, tenant_id=TENANT, user_id="reader",
        name="evaluation_status", arguments={"workflow_id": "hot-news-data-loop-" + "a" * 64},
        permissions=frozenset({"data-loop:read"}))
    assert first["source_tenant"] != second["source_tenant"]
    assert third["job"]["id"] != first["job"]["id"]
    assert evaluation["version"] == 4 and len(tools.calls) == 4
    with pytest.raises(ConversationToolDenied, match="arguments_invalid"):
        await reader.read(tools=tools, tenant_id=TENANT, user_id="reader", name="writing_job_status",
            arguments={"job_id": JOB_ID, "tenant_id": "other"}, permissions=frozenset({"writing:read"}))
    await reader.close()


@pytest.mark.asyncio
async def test_user_memory_is_never_admitted_to_shared_cache():
    tools, reader = Tools(), ReceiptReadCoordinator()
    with pytest.raises(ConversationToolDenied, match="not_enabled"):
        await reader.read(tools=tools, tenant_id=TENANT, user_id="reader", name="memory_status",
            arguments={"memory_id": str(uuid4()), "kind": "short_term"}, permissions=frozenset({"memory:admin"}))
    assert tools.calls == [] and not reader._cache and not reader._inflight
    await reader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments,permissions,ttl", [
    ("writing_job_status", {"job_id": JOB_ID}, frozenset({"writing:read"}), 3),
    ("evaluation_status", {"workflow_id": "hot-news-data-loop-" + "a" * 64}, frozenset({"data-loop:read"}), 15),
])
async def test_fixed_ttl_refreshes_at_boundary_without_extending_on_cache_hits(name, arguments, permissions, ttl):
    now = [0.0]
    tools, reader = Tools(), ReceiptReadCoordinator(clock=lambda: now[0])
    kwargs = dict(tools=tools, tenant_id=TENANT, user_id="reader", name=name, arguments=arguments, permissions=permissions)
    await reader.read(**kwargs)
    now[0] = ttl - .01
    await reader.read(**kwargs)
    assert len(tools.calls) == 1
    now[0] = ttl
    await reader.read(**kwargs)
    assert len(tools.calls) == 2
    await reader.close()


@pytest.mark.asyncio
async def test_lru_and_inflight_limits_are_bounded_and_same_key_can_join_when_full():
    tools, reader = Tools(), ReceiptReadCoordinator(max_entries=2, max_inflight=1)
    ids = [str(uuid4()) for _ in range(3)]
    for job in ids[:2]:
        await read(reader, tools, job=job)
    await read(reader, tools, job=ids[0])
    await read(reader, tools, job=ids[2])
    assert len(reader._cache) == 2 and (TENANT, "writing_job_status", ids[1]) not in reader._cache
    await read(reader, tools, job=ids[1])
    assert len(tools.calls) == 4
    tools.release.clear()
    pending_id = str(uuid4())
    first = asyncio.create_task(read(reader, tools, job=pending_id))
    await asyncio.sleep(0)
    second = asyncio.create_task(read(reader, tools, job=pending_id))
    await asyncio.sleep(0)
    with pytest.raises(ReceiptReadUnavailable, match="budget exhausted"):
        await read(reader, tools, job=str(uuid4()))
    assert len(reader._inflight) == 1
    tools.release.set()
    await asyncio.gather(first, second)
    assert not reader._inflight
    await reader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_result", [
    {"not_found": True}, {"error_code": "source_unavailable"},
    {"admission": {"state": "unknown", "error_code": "admission_snapshot_unavailable"}},
])
async def test_failed_or_unknown_snapshots_are_not_cached(failure_result):
    tools, reader = Tools(), ReceiptReadCoordinator()
    tools.result = failure_result
    await read(reader, tools)
    await read(reader, tools)
    assert len(tools.calls) == 2 and not reader._cache and not reader._inflight
    await reader.close()


@pytest.mark.asyncio
async def test_exception_singleflight_cleans_up_and_next_request_retries_fresh_read():
    tools, reader = Tools(), ReceiptReadCoordinator()
    tools.release.clear()
    tools.failure = RPCError("test only", RPCStatusCode.UNAVAILABLE, b"")
    tasks = [asyncio.create_task(read(reader, tools)) for _ in range(10)]
    await tools.started.wait()
    await asyncio.sleep(0)
    tools.release.set()
    errors = await asyncio.gather(*tasks, return_exceptions=True)
    assert len(tools.calls) == 1 and all(isinstance(error, RPCError) for error in errors)
    assert not reader._inflight and not reader._cache
    tools.failure = None
    await read(reader, tools)
    assert len(tools.calls) == 2
    await reader.close()


@pytest.mark.asyncio
async def test_cancelled_first_caller_does_not_cancel_shared_read_and_close_cleans_tasks():
    tools, reader = Tools(), ReceiptReadCoordinator()
    tools.release.clear()
    abandoned = asyncio.create_task(read(reader, tools))
    await tools.started.wait()
    survivor = asyncio.create_task(read(reader, tools, user="other-authorized-user"))
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    assert tools.cancelled is False and len(reader._inflight) == 1
    tools.release.set()
    assert (await survivor)["job"]["id"] == JOB_ID
    assert len(tools.calls) == 1
    tools.release.clear()
    shutdown_waiter = asyncio.create_task(read(reader, tools, job=str(uuid4())))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await reader.close()
    with pytest.raises(asyncio.CancelledError):
        await shutdown_waiter
    assert tools.cancelled and not reader._inflight and not reader._cache
    with pytest.raises(ReceiptReadUnavailable, match="closed"):
        await read(reader, tools)


@pytest.mark.asyncio
async def test_http_same_tenant_users_share_read_but_403_disabled_and_cross_tenant_never_reuse():
    tools, jobs = tools_with_reader()
    source = jobs.get
    started, release = asyncio.Event(), asyncio.Event()
    reads = []

    async def blocked(*args, **kwargs):
        reads.append(kwargs)
        started.set()
        await release.wait()
        return await source(*args, **kwargs)

    jobs.get = blocked
    client, headers = receipt_client(tools)
    path = f"/api/v1/conversations/receipts/writing-jobs/{JOB_ID}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(client.app), base_url="http://local.test") as http:
        approved = {**headers, "X-Writing-Roles": "writing:read"}
        requests = [asyncio.create_task(http.get(path, headers={**approved, "X-User-ID": str(uuid4())})) for _ in range(12)]
        await started.wait()
        await asyncio.sleep(0)
        assert (await http.get(path, headers=headers)).status_code == 403
        release.set()
        responses = await asyncio.gather(*requests)
        assert all(response.status_code == 200 and response.json() == responses[0].json() for response in responses)
        assert len(reads) == 1
        assert (await http.get(path, headers={**approved, "X-Tenant-ID": str(uuid4())})).status_code == 404
        assert len(reads) == 2
        assert (await http.get(path, headers=headers)).status_code == 403
        tools._writing_reader = None
        assert (await http.get(path, headers=approved)).status_code == 503
        assert len(reads) == 2
    await client.app.state.conversation_receipt_reads.close()


@pytest.mark.asyncio
async def test_api_lifespan_closes_receipt_cache_before_dependency_cleanup(monkeypatch):
    import app.bootstrap.api_lifespan as main
    settings = SimpleNamespace(model_runtime_backend="unused", temporal_address="unused",
        temporal_namespace="unused", redis_url="unused", conversation_enabled=False,
        conversation_async_admission_enabled=False, audience_enabled=False)
    database = SimpleNamespace(close=AsyncMock())
    redis = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "Database", lambda config: database)
    monkeypatch.setattr(main.Client, "connect", AsyncMock(return_value=object()))
    monkeypatch.setattr(main.Redis, "from_url", lambda *args, **kwargs: redis)
    for name in ("OrchestratorService", "DataLoopOrchestrator", "RedisProgressReader", "RedisHotNewsEventStream",
                 "S3ArtifactStore", "S3EvaluationDatasetArtifactStore", "S3DataLoopArtifactStore", "CmsPublisher"):
        monkeypatch.setattr(main, name, lambda *args, **kwargs: object())
    monkeypatch.setattr(main, "close_knowledge_search_index", AsyncMock())
    application = FastAPI()
    tools = Tools()
    tools.release.clear()
    async with main.lifespan(application):
        reader = application.state.conversation_receipt_reads
        waiter = asyncio.create_task(read(reader, tools))
        await tools.started.wait()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert reader._closed and not reader._inflight and not reader._cache
    assert tools.cancelled and database.close.await_count == 1 and redis.aclose.await_count == 1


@pytest.mark.parametrize("values", [{"max_entries": 0}, {"max_inflight": True}, {"max_entries": -1}])
def test_invalid_bounds_cannot_disable_boundedness(values):
    with pytest.raises(ValueError):
        ReceiptReadCoordinator(**values)
