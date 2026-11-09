"""Exercise the real shared hotspot command with bounded infrastructure doubles."""

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api.dependencies import DataLoopPrincipal, HotNewsPermission
from app.sql_assistant.hot_news_binding import HotNewsSqlBindingStore, HotNewsSqlBindingPersistenceError
from app.sql_assistant.service import SqlAssistantError
from app.sql_assistant.warehouse import WINDOW_START
from simulation.hot_news_e2e_support import E2E_TENANT_ID
from tests.test_native_hot_news_runtime import native_spec
from tests.test_sql_assistant_service import harness, request, USER
from tests.test_sql_candidate_service import Candidate, configure


class Database:
    @asynccontextmanager
    async def session(self):
        yield object()


class Bindings:
    def __init__(self):
        self.rows = {}
        self.plan_once = AsyncMock(side_effect=self._plan_once)
        self.get_or_claim = AsyncMock(side_effect=self._get_or_claim)
        self.require = AsyncMock(side_effect=self._require)

    async def _get_or_claim(self, **values):
        row = self.rows.setdefault(values["run_key"], values)
        return HotNewsSqlBindingStore._validate_binding(row, values, canonical_query=True)

    async def _plan_once(self, *, build_preview, **values):
        if values["run_key"] in self.rows:
            return HotNewsSqlBindingStore._validate_binding(self.rows[values["run_key"]], values, canonical_query=True)
        value = await build_preview()
        return await self._get_or_claim(**{**values, "query_id": value.query_id})

    async def _require(self, **values):
        return HotNewsSqlBindingStore._validate_binding(self.rows[values["run_key"]], values, canonical_query=False)


def state(h, monkeypatch, bindings):
    import simulation.local_simulation as shared
    spec = native_spec()
    bundle = SimpleNamespace(spec=spec, bundle_version="existing-bundle")
    monkeypatch.setattr(shared, "PostgresProductionBundleRepository", lambda _: SimpleNamespace(
        get_active_bundle=AsyncMock(return_value=bundle),
    ))
    h.service.hot_news_bindings = bindings
    completed = {}

    async def start_workflow(_, run, **kwargs):
        assert kwargs["id"] == run.idempotency_key

        async def result():
            # Simulate Temporal replay while retaining actual service execution
            # and its claim, snapshot, guard, hash and result cache checks.
            data = await h.service.execute_for_hot_news(run.sql_query_id, request=run)
            return completed.setdefault(run.idempotency_key, {
                "run_id": str(uuid4()), "idempotency_key": run.idempotency_key, "status": "completed",
                "fetched_record_count": data.row_count, "metric_snapshot_count": data.row_count,
                "ranked_news_count": data.row_count, "analyzed_news_count": data.row_count,
            })

        return SimpleNamespace(result=result)

    return SimpleNamespace(
        settings=SimpleNamespace(
            hot_news_runtime_manifest_json=json.dumps([spec.model_dump(mode="json")]),
            model_runtime_config_path="deploy/model-runtime.local.yml",
            sql_assistant_scenarios_path=str(h.scenarios_path), temporal_hot_news_task_queue="query-test",
        ),
        sql_assistant=h.service, database=Database(),
        temporal=SimpleNamespace(start_workflow=AsyncMock(side_effect=start_workflow)),
    )


async def execute(state, h):
    from simulation.local_simulation import execute_local_hot_news_query
    return await execute_local_hot_news_query(
        state=state,
        principal=DataLoopPrincipal(tenant_id=UUID(E2E_TENANT_ID), user_id=UUID(USER),
                                   permissions=frozenset({HotNewsPermission.ADMIN.value})),
        body=request(question="查询点击量最高的前5条新闻", window_end=WINDOW_START + timedelta(hours=1)),
        expected_intent=h.agent.intent,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dbgpt_candidate", "dbgpt_shadow"])
async def test_actual_command_reuses_candidate_generation_and_single_saved_query(harness, monkeypatch, mode):
    candidate = configure(harness, mode)
    bindings = Bindings()
    current = state(harness, monkeypatch, bindings)
    first = await execute(current, harness)
    replay = await execute(current, harness)
    assert first == replay
    assert bindings.plan_once.await_count == 2
    bindings.get_or_claim.assert_not_awaited()
    assert len(candidate.calls) == 1 and len(harness.store.snapshots) == 1
    assert len(harness.warehouse.queries) == 1
    assert harness.agent.calls == []
    first_run = current.temporal.start_workflow.call_args_list[0].args[1]
    second_run = current.temporal.start_workflow.call_args_list[1].args[1]
    assert first_run == second_run
    snapshot = harness.store.snapshots[first.sql_query_id]
    assert snapshot["preview"]["generation"]["mode"] == mode
    assert snapshot["preview"]["generation"]["status"] == "validated"


@pytest.mark.asyncio
async def test_native_command_keeps_legacy_binding_path(harness, monkeypatch):
    bindings = Bindings()
    current = state(harness, monkeypatch, bindings)
    result = await execute(current, harness)
    bindings.plan_once.assert_not_awaited()
    bindings.get_or_claim.assert_awaited_once()
    assert len(harness.warehouse.queries) == 1
    assert harness.store.snapshots[result.sql_query_id]["preview"]["generation"] is None


@pytest.mark.asyncio
async def test_rejected_candidate_never_binds_or_starts_temporal(harness, monkeypatch):
    candidate = configure(harness, candidate=Candidate(lambda sql: sql.replace("ORDER BY clicks DESC", "ORDER BY ctr DESC")))
    bindings = Bindings()
    current = state(harness, monkeypatch, bindings)
    with pytest.raises(HTTPException) as error:
        await execute(current, harness)
    assert error.value.status_code == 422
    assert bindings.rows == {} and len(candidate.calls) == 1
    current.temporal.start_workflow.assert_not_awaited()
    snapshot = next(iter(harness.store.snapshots.values()))
    assert snapshot["preview"]["generation"]["status"] == "rejected"
    with pytest.raises(SqlAssistantError):
        await harness.service.execute(snapshot["preview"]["query_id"], tenant_id=E2E_TENANT_ID, user_id=USER)
    assert harness.warehouse.queries == []


@pytest.mark.asyncio
async def test_busy_candidate_run_returns_503_without_another_model_call(harness, monkeypatch):
    candidate = configure(harness)
    bindings = Bindings()
    bindings.plan_once.side_effect = HotNewsSqlBindingPersistenceError("in progress")
    current = state(harness, monkeypatch, bindings)
    with pytest.raises(HTTPException) as error:
        await execute(current, harness)
    assert error.value.status_code == 503
    assert candidate.calls == [] and harness.store.snapshots == {}
    current.temporal.start_workflow.assert_not_awaited()
