"""Candidate selection exercises the existing preview/execute trust boundary."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from unittest.mock import AsyncMock

import pytest

from app.sql_assistant.candidate import SqlCandidateConfig, SqlCandidateError, SqlCandidateResult
from app.sql_assistant.planner import compile_query
from app.sql_assistant.service import SqlAssistantService, SqlAssistantError, _snapshot_fingerprint
from tests.test_sql_assistant_service import harness, request, TENANT, USER, hot_news_request
from app.sql_assistant.warehouse import WINDOW_START


class Candidate:
    def __init__(self, transform=lambda sql: sql.lower(), error=None):
        self.calls = []
        self.transform, self.error = transform, error

    def identity(self):
        return {"adapter_version": "fixture-v1", "model_route": "fixture-model"}

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SqlCandidateResult("proposed", self.transform(compile_query(kwargs["intent"], kwargs["scenario"])),
                                  None, {"attempts": 1, "elapsed_ms": 2, "usage": {"total_tokens": 30}})


def configure(h, mode="dbgpt_candidate", candidate=None):
    candidate = candidate or Candidate()
    h.service = SqlAssistantService(
        agent=h.agent, store=h.store, warehouse_factory=h.service.warehouse_factory,
        scenarios_path=str(h.scenarios_path), model_provider="fixture",
        candidate_config=SqlCandidateConfig(mode=mode), candidate_generator=candidate,
    )
    return candidate


async def preview(h):
    return await h.service.preview(request(window_end=WINDOW_START + timedelta(hours=1)), tenant_id=TENANT, user_id=USER)


@pytest.mark.asyncio
async def test_candidate_format_variation_executes_once_and_reuses_result(harness):
    candidate = configure(harness)
    value = await preview(harness)
    assert value.generation.status == "validated"
    assert value.sql.startswith("select")
    assert not harness.warehouse.queries
    assert len(candidate.calls) == 1
    result = await harness.service.execute(value.query_id, tenant_id=TENANT, user_id=USER)
    assert harness.warehouse.queries[0].sql == value.sql
    assert await harness.service.execute(value.query_id, tenant_id=TENANT, user_id=USER) == result
    assert len(harness.warehouse.queries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dbgpt_candidate", "dbgpt_shadow"])
@pytest.mark.parametrize("transform", [
    lambda sql: sql.replace("ORDER BY clicks DESC", "ORDER BY ctr DESC"),
    lambda sql: sql.replace("ORDER BY clicks DESC", "ORDER BY clicks ASC"),
    lambda sql: sql.replace("tenant_id = :tenant_id", "tenant_id = 'foreign'"),
    lambda sql: sql.replace("dw.news_behavior_aggregate", "dw.secret"),
    lambda sql: sql + "; DELETE FROM dw.news_behavior_aggregate",
])
async def test_wrong_or_unsafe_sql_never_reaches_warehouse(harness, mode, transform):
    configure(harness, mode, Candidate(transform))
    if mode == "dbgpt_candidate":
        with pytest.raises(SqlAssistantError, match="SQL候选"):
            await preview(harness)
        snapshot = next(iter(harness.store.snapshots.values()))
        with pytest.raises(SqlAssistantError):
            await harness.service.execute(snapshot["preview"]["query_id"], tenant_id=TENANT, user_id=USER)
        assert not harness.warehouse.queries
    else:
        value = await preview(harness)
        assert value.generation.status == "rejected"
        assert value.generation.candidate_sql_hash is not None
        assert "ORDER BY clicks DESC" in value.sql
        await harness.service.execute(value.query_id, tenant_id=TENANT, user_id=USER)
        assert len(harness.warehouse.queries) == 1
        assert harness.warehouse.queries[0].sql == value.sql


@pytest.mark.asyncio
async def test_shadow_success_keeps_native_sql_and_single_execution(harness):
    configure(harness, "dbgpt_shadow")
    value = await preview(harness)
    assert value.generation.status == "validated"
    assert value.sql.startswith("SELECT")
    assert value.generation.candidate_sql_hash != value.sql_hash
    await harness.service.execute(value.query_id, tenant_id=TENANT, user_id=USER)
    assert len(harness.warehouse.queries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dbgpt_candidate", "dbgpt_shadow"])
async def test_generation_failure_is_audited_and_never_silently_candidate_success(harness, mode):
    configure(harness, mode, Candidate(error=SqlCandidateError("deadline_exceeded", metadata={"attempts": 1})))
    if mode == "dbgpt_candidate":
        with pytest.raises(SqlAssistantError):
            await preview(harness)
    else:
        value = await preview(harness)
        await harness.service.execute(value.query_id, tenant_id=TENANT, user_id=USER)
    snapshot = next(iter(harness.store.snapshots.values()))
    assert snapshot["preview"]["generation"]["status"] == "failed"
    assert len(harness.warehouse.queries) == (1 if mode == "dbgpt_shadow" else 0)


@pytest.mark.asyncio
async def test_cancel_does_not_return_a_candidate_or_execute_sql(harness):
    configure(harness, candidate=Candidate(error=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await preview(harness)
    assert not harness.store.snapshots and not harness.warehouse.queries


@pytest.mark.asyncio
async def test_native_worker_can_resume_frozen_candidate_after_config_rollback(harness):
    configure(harness)
    value = await preview(harness)
    # New requests would now be native, but the worker validates the frozen
    # candidate using retained comparator assets without another model call.
    configure(harness, "native", Candidate(error=AssertionError("must not generate")))
    harness.service.hot_news_bindings = AsyncMock()
    harness.scenarios_path.unlink()
    result = await harness.service.execute_for_hot_news(value.query_id, request=hot_news_request(value))
    assert result.row_count == 1 and harness.warehouse.queries[0].sql == value.sql


@pytest.mark.asyncio
async def test_legacy_snapshot_without_generation_remains_executable(harness):
    value = await preview(harness)
    snapshot = harness.store.snapshots[value.query_id]
    snapshot["preview"].pop("generation", None)
    snapshot["fingerprint"] = _snapshot_fingerprint(snapshot)
    configure(harness)
    result = await harness.service.execute(value.query_id, tenant_id=TENANT, user_id=USER)
    assert result.row_count == 1


@pytest.mark.asyncio
async def test_tampering_provenance_is_detected_before_execution(harness):
    configure(harness)
    value = await preview(harness)
    snapshot = harness.store.snapshots[value.query_id]
    snapshot["preview"]["generation"]["identity"]["generator"]["model_route"] = "other"
    with pytest.raises(SqlAssistantError, match="完整性"):
        await harness.service.execute(value.query_id, tenant_id=TENANT, user_id=USER)
    assert not harness.warehouse.queries


@pytest.mark.asyncio
async def test_snapshot_cannot_change_sort_even_with_new_sql_digest(harness):
    configure(harness)
    value = await preview(harness)
    snapshot = harness.store.snapshots[value.query_id]
    plan = snapshot["preview"]
    plan["sql"] = plan["sql"].replace("order by clicks desc", "order by ctr desc")
    plan["sql_hash"] = sha256(plan["sql"].encode()).hexdigest()
    plan["generation"]["candidate_sql_hash"] = plan["sql_hash"]
    snapshot["fingerprint"] = _snapshot_fingerprint(snapshot)
    with pytest.raises(SqlAssistantError, match="批准"):
        await harness.service.execute(value.query_id, tenant_id=TENANT, user_id=USER)
    assert not harness.warehouse.queries


def test_native_does_not_require_generator_or_include_new_run_identity(harness):
    assert harness.service.generation_identity() is None
    configure(harness, "native", Candidate(error=AssertionError()))
    assert harness.service.generation_identity() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dbgpt_candidate", "dbgpt_shadow"])
async def test_adapter_bug_is_sanitized_audited_and_shadow_remains_native(harness, mode):
    configure(harness, mode, Candidate(error=RuntimeError("private connection detail")))
    if mode == "dbgpt_candidate":
        with pytest.raises(SqlAssistantError) as error:
            await preview(harness)
        assert "private" not in str(error.value)
    else:
        value = await preview(harness)
        await harness.service.execute(value.query_id, tenant_id=TENANT, user_id=USER)
        assert len(harness.warehouse.queries) == 1
    trace = next(iter(harness.store.snapshots.values()))["preview"]["generation"]
    assert trace["status"] == "failed" and trace["reason_code"] == "candidate_internal_error"
    assert "private" not in str(trace)
