from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import app.services.data_loop.step_handler as handler_module
from app.schemas.production_bundle import (
    EvaluationCohortMetrics,
    EvaluationMetricSet,
    EvaluationSuiteMetrics,
)
from app.services.data_loop.artifacts import DataLoopArtifactReceipt
from app.services.data_loop.offline_replay import OfflineReplayResult
from app.services.data_loop.step_handler import (
    EvaluationGatePolicyRegistry,
    HotNewsDataLoopStepHandler,
)
from app.services.production_bundle import (
    ProductionBundleDomainService,
    ProductionBundleRuleViolation,
)
from app.workflows.data_loop_contracts import DataLoopStepCommand
from tests.test_dataset_freezer import (
    CASE_A,
    FakeArtifactStore,
    FakeRepository,
    _snapshot,
)
from tests.test_offline_replay import manifest
from tests.test_production_bundle import NOW, active_bundle, proposal


class FakeDatabase:
    def __init__(self):
        self.session_value = SimpleNamespace(rollback=AsyncMock())

    @asynccontextmanager
    async def session(self):
        yield self.session_value


def handler(**overrides) -> HotNewsDataLoopStepHandler:
    values = {
        "database": FakeDatabase(),
        "dataset_artifact_store": FakeArtifactStore(),
        "data_loop_artifact_store": SimpleNamespace(put_json=AsyncMock()),
        "offline_replay": SimpleNamespace(),
        "error_attribution_runner": None,
    }
    values.update(overrides)
    return HotNewsDataLoopStepHandler(**values)


def test_gate_policy_registry_refuses_unversioned_request_thresholds() -> None:
    registry = EvaluationGatePolicyRegistry()

    assert registry.get("hot-news-gate-v1").require_previous_experiment_baseline is False
    assert (
        registry.get("hot-news-gate-double-baseline-v1")
        .require_previous_experiment_baseline
        is True
    )
    with pytest.raises(ValueError, match="unknown evaluation"):
        registry.get("use-these-request-thresholds")


@pytest.mark.asyncio
async def test_freeze_step_selects_only_approved_window_cases(
    monkeypatch,
) -> None:
    repository = FakeRepository((_snapshot(CASE_A, "a"),))
    repository.list_approved_case_ids_for_window = AsyncMock(
        return_value=(CASE_A,)
    )
    monkeypatch.setattr(
        handler_module,
        "PostgresEvaluationDatasetRepository",
        lambda session: repository,
    )
    start = datetime(2026, 9, 8, tzinfo=timezone.utc)
    command = DataLoopStepCommand(
        tenant_id="tenant-1",
        run_idempotency_key="data-loop-run-1",
        step_type="freeze_dataset",
        step_key="freeze-v1",
        inputs={
            "window_start": start.isoformat(),
            "window_end": (start + timedelta(days=1)).isoformat(),
            "dataset_name": "daily-feedback",
            "dataset_version": "2026-09-08.v1",
        },
    )

    database = FakeDatabase()
    outcome = await handler(database=database).execute(command)

    assert outcome.status == "completed"
    assert outcome.values["case_count"] == 1
    repository.list_approved_case_ids_for_window.assert_awaited_once()
    assert repository.saved_manifest.dataset_layer == "fresh_bad_case"
    database.session_value.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluate_step_loads_three_cohorts_and_records_gate_snapshot(
    monkeypatch,
) -> None:
    dataset_ids = {
        "golden": UUID("20000000-0000-4000-8000-000000000001"),
        "fresh_bad_case": UUID("20000000-0000-4000-8000-000000000002"),
        "high_risk_regression": UUID(
            "20000000-0000-4000-8000-000000000003"
        ),
    }
    manifests = {
        dataset_id: manifest(layer, dataset_id)
        for layer, dataset_id in dataset_ids.items()
    }

    class FakeFreezer:
        def __init__(self, **kwargs):
            pass

        async def load_manifest(self, *, tenant_id, dataset_id):
            assert tenant_id == "tenant-1"
            return manifests[dataset_id]

    monkeypatch.setattr(handler_module, "EvaluationDatasetFreezer", FakeFreezer)
    monkeypatch.setattr(
        handler_module,
        "PostgresEvaluationDatasetRepository",
        lambda session: object(),
    )

    base = active_bundle()
    candidate = ProductionBundleDomainService().propose_candidate(
        base_bundle=base,
        command=proposal(base),
        now=NOW,
    )
    repository = SimpleNamespace(
        get_evaluation_by_idempotency_key=AsyncMock(return_value=None),
        get_candidate_for_update=AsyncMock(return_value=candidate),
        get_bundle_for_update=AsyncMock(return_value=base),
    )
    monkeypatch.setattr(
        handler_module,
        "PostgresProductionBundleRepository",
        lambda session: repository,
    )
    metric = EvaluationMetricSet(
        case_count=1,
        passed_count=1,
        pass_rate=1,
        evidence_precision=1,
        metric_coverage=1,
        critical_failures=0,
    )
    suite = EvaluationSuiteMetrics(
        golden=EvaluationCohortMetrics(
            dataset_id=dataset_ids["golden"],
            layer="golden",
            candidate=metric,
            online_baseline=metric,
        ),
        fresh_bad_case=EvaluationCohortMetrics(
            dataset_id=dataset_ids["fresh_bad_case"],
            layer="fresh_bad_case",
            candidate=metric,
            online_baseline=metric,
        ),
        high_risk_regression=EvaluationCohortMetrics(
            dataset_id=dataset_ids["high_risk_regression"],
            layer="high_risk_regression",
            candidate=metric,
            online_baseline=metric,
        ),
    )
    offline = SimpleNamespace(
        evaluator_version="offline-v1",
        evaluate=AsyncMock(
            return_value=OfflineReplayResult(
                suite_metrics=suite,
                artifact_payload={"schema_version": "1.0"},
            )
        ),
    )
    artifact_store = SimpleNamespace(
        put_json=AsyncMock(
            return_value=DataLoopArtifactReceipt(
                storage_uri="s3://bucket/evaluation.json",
                content_sha256="a" * 64,
                content_size=10,
            )
        )
    )
    evaluation_run = SimpleNamespace(
        id=UUID("30000000-0000-4000-8000-000000000001"),
        artifact_uri="s3://bucket/evaluation.json",
        artifact_sha256="a" * 64,
        gate_decision=SimpleNamespace(passed=True, failures=()),
    )
    bundle_service = SimpleNamespace(
        record_evaluation=AsyncMock(
            return_value=SimpleNamespace(
                evaluation_run=evaluation_run,
                created=True,
            )
        )
    )
    service = handler(
        offline_replay=offline,
        data_loop_artifact_store=artifact_store,
        bundle_service=bundle_service,
    )
    command = DataLoopStepCommand(
        tenant_id="tenant-1",
        run_idempotency_key="data-loop-run-1",
        step_type="evaluate_candidate",
        step_key="evaluate-v1",
        inputs={
            "candidate_id": str(candidate.id),
            "dataset_id": str(dataset_ids["fresh_bad_case"]),
            "golden_dataset_id": str(dataset_ids["golden"]),
            "high_risk_regression_dataset_id": str(
                dataset_ids["high_risk_regression"]
            ),
            "previous_experiment_candidate_id": None,
            "evaluation_policy_version": "hot-news-gate-v1",
            "attribution_status": "degraded",
        },
    )

    outcome = await service.execute(command)

    assert outcome.values["gate_passed"] is True
    assert offline.evaluate.await_args.kwargs["manifests"] == (
        manifests[dataset_ids["golden"]],
        manifests[dataset_ids["fresh_bad_case"]],
        manifests[dataset_ids["high_risk_regression"]],
    )
    recorded = bundle_service.record_evaluation.await_args.kwargs["command"]
    assert recorded.suite_metrics == suite
    assert recorded.gate_policy.policy_version == "hot-news-gate-v1"


@pytest.mark.asyncio
async def test_activation_replay_refuses_a_bundle_deactivated_by_rollback(
    monkeypatch,
) -> None:
    candidate_id = UUID("40000000-0000-4000-8000-000000000001")
    evaluation_id = UUID("40000000-0000-4000-8000-000000000002")
    bundle_id = UUID("40000000-0000-4000-8000-000000000003")
    repository = SimpleNamespace(
        get_decision_by_idempotency_key=AsyncMock(
            return_value=SimpleNamespace(
                action="activate",
                candidate_id=candidate_id,
                evaluation_run_id=evaluation_id,
                to_bundle_id=bundle_id,
                actor_id="reviewer-1",
                reason="approved after evaluation",
            )
        ),
        get_bundle_for_update=AsyncMock(
            return_value=SimpleNamespace(
                id=bundle_id,
                bundle_version="candidate-v2",
                status="inactive",
            )
        ),
    )
    monkeypatch.setattr(
        handler_module,
        "PostgresProductionBundleRepository",
        lambda session: repository,
    )
    command = DataLoopStepCommand(
        tenant_id="tenant-1",
        run_idempotency_key="data-loop-run-1",
        step_type="activate_bundle",
        step_key="activate-bundle-v1",
        inputs={
            "candidate_id": str(candidate_id),
            "evaluation_run_id": str(evaluation_id),
            "actor_id": "reviewer-1",
            "reason": "approved after evaluation",
            "idempotency_key": "approval-request-1:activate",
        },
    )

    with pytest.raises(ProductionBundleRuleViolation, match="no longer active"):
        await handler().execute(command)
