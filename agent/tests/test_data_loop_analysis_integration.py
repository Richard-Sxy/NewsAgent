"""Analysis wiring: durable Activity evidence, backend limits and read permissions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from pydantic import ValidationError

import app.services.data_loop.step_handler as handler_module
import app.data_loop_worker as worker_module
from app.activities.data_loop import DataLoopActivities
from app.api.dependencies import DataLoopPermission, get_data_loop_artifact_store
from app.config import Settings
from app.data_analysis.factory import create_analysis_runner
from app.data_loop_worker import DataLoopWorkerRuntime
from app.domain.errors import ArtifactIntegrityError
from app.schemas.evaluation_dataset import canonical_json_bytes, canonical_json_sha256
from app.services.data_loop.artifacts import DataLoopArtifactReceipt
from app.services.data_loop.dataset_analysis import DATASET_ANALYSIS_REPORT_VERSION
from app.services.production_bundle import ProductionBundleRuleViolation
from app.workflows.data_loop_contracts import DataLoopStepCommand, DataLoopWorkflowSnapshot
from tests.test_data_loop_api import TENANT_ID, client_with
from tests.test_data_loop_step_handler import handler
from tests.test_data_loop_step_handler import FakeDatabase
from tests.test_data_loop_dataset_analysis import detail as source_detail, manifest as source_manifest
from tests.test_offline_replay import manifest


DATASET_ID = UUID("20000000-0000-4000-8000-000000000002")
SETTINGS_BASE = {
    "database_url": "postgresql+psycopg://test:test@localhost/test",
    "redis_url": "redis://localhost/0", "temporal_address": "localhost:7233",
    "temporal_namespace": "default", "artifact_bucket": "test-artifacts",
    "model_runtime_config_path": "unused.yml",
}


def report_for(frozen, *, status="completed"):
    return {
        "schema_version": "1.0", "report_version": DATASET_ANALYSIS_REPORT_VERSION,
        "tenant_id": frozen.tenant_id, "dataset_id": str(frozen.dataset_id),
        "dataset_sha256": frozen.content_sha256, "analysis_status": status,
        "scope": "referenced_ranked_runs", "summary": {
            "case_count": 1, "source_run_count": 1, "analyzed_run_count": 1,
            "covered_case_count": 1, "unavailable_case_count": 0,
        },
        "source_runs": [{"evidence": "large report stays outside History"}],
    }


class MemoryReports:
    def __init__(self):
        self.saved = {}

    async def find_json(self, **key):
        return self.saved.get(tuple(key.values()))

    async def put_json_once(self, *, payload, **key):
        identity = tuple(key.values())
        digest = canonical_json_sha256(payload)
        receipt = DataLoopArtifactReceipt(
            f"s3://bucket/dataset-analysis/{digest}.json", digest, len(canonical_json_bytes(payload)),
        )
        return self.saved.setdefault(identity, (receipt, payload))


def frozen_step(monkeypatch):
    frozen = manifest("fresh_bad_case", DATASET_ID)
    case = frozen.cases[0]
    frozen = frozen.model_copy(update={"cases": (case.model_copy(update={
        "lineage": case.lineage.model_copy(update={"run_id": UUID("40000000-0000-4000-8000-000000000001")}),
    }),)})
    class Freezer:
        def __init__(self, **kwargs):
            pass

        async def load_manifest(self, *, tenant_id, dataset_id):
            assert tenant_id == frozen.tenant_id
            assert dataset_id == frozen.dataset_id
            return frozen

    monkeypatch.setattr(handler_module, "EvaluationDatasetFreezer", Freezer)
    monkeypatch.setattr(handler_module, "PostgresEvaluationDatasetRepository", lambda session: object())
    return frozen, DataLoopStepCommand(
        tenant_id=frozen.tenant_id, run_idempotency_key="analysis-run-1",
        step_type="analyze_dataset", step_key="analyze-dataset-v1",
        inputs={"dataset_id": str(DATASET_ID)},
    )


@pytest.mark.asyncio
async def test_activity_retry_reuses_durable_report_even_after_feature_is_disabled(monkeypatch):
    frozen, command = frozen_step(monkeypatch)
    analysis = SimpleNamespace(analyze=AsyncMock(return_value=report_for(frozen)))
    store = MemoryReports()
    first = await handler(dataset_analysis=analysis, data_loop_artifact_store=store).execute(command)
    retried = await handler(dataset_analysis=None, data_loop_artifact_store=store).execute(command)
    assert retried == first
    assert first.values["analysis_status"] == "completed"
    assert set(first.values) == {"analysis_status", "summary", "artifact_uri", "artifact_sha256"}
    analysis.analyze.assert_awaited_once_with(tenant_id=frozen.tenant_id, manifest=frozen)


@pytest.mark.asyncio
async def test_disabled_activity_is_explicit_and_receipt_survives_later_enable(monkeypatch):
    frozen, command = frozen_step(monkeypatch)
    store = MemoryReports()
    first = await handler(data_loop_artifact_store=store).execute(command)
    analysis = SimpleNamespace(analyze=AsyncMock(return_value=report_for(frozen)))
    retried = await handler(dataset_analysis=analysis, data_loop_artifact_store=store).execute(command)
    assert first == retried
    assert first.values["analysis_status"] == "disabled"
    assert first.values["summary"]["covered_case_count"] == 0
    assert first.values["summary"]["unavailable_case_count"] == 1
    analysis.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_identity_failure_is_never_saved(monkeypatch):
    frozen, command = frozen_step(monkeypatch)
    report = report_for(frozen)
    report["tenant_id"] = "another-tenant"
    store = MemoryReports()
    with pytest.raises(ProductionBundleRuleViolation, match="identity mismatch"):
        await handler(dataset_analysis=SimpleNamespace(analyze=AsyncMock(return_value=report)),
                      data_loop_artifact_store=store).execute(command)
    assert not store.saved


@pytest.mark.parametrize("changes,message", [
    ({"data_analysis_backend": "process", "environment": "production"}, "Process analysis isolation"),
    ({"data_analysis_backend": "service", "environment": "test"}, "Service analysis requires"),
    ({"data_analysis_backend": "service", "environment": "production",
     "data_analysis_service_url": "https://analysis.example", "data_analysis_service_token": "a" * 32,
     "data_analysis_service_require_os_limits": False}, "Production analysis service requires"),
])
def test_dataloop_only_backend_cannot_bypass_runtime_safety(changes, message):
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **SETTINGS_BASE,
                 conversation_data_analysis_enabled=False, data_loop_data_analysis_enabled=True, **changes)


def test_factory_enables_dataloop_independently_of_conversation():
    settings = Settings(_env_file=None, **SETTINGS_BASE, environment="test",
                        conversation_data_analysis_enabled=False, data_loop_data_analysis_enabled=True)
    with patch("app.data_analysis.factory.AnalysisRunner") as factory:
        assert create_analysis_runner(settings) is None
        assert create_analysis_runner(settings, enabled=settings.data_loop_data_analysis_enabled) is factory.return_value
        assert factory.call_args.kwargs["max_rows"] == settings.data_analysis_max_rows


@pytest.mark.asyncio
async def test_worker_closes_model_and_database_even_if_runner_close_fails():
    ports, database = SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())
    runtime = DataLoopWorkerRuntime(handler=None, model_ports=ports, database=database,
                                    analysis_runner=SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close"))))
    with pytest.raises(RuntimeError, match="close"):
        await runtime.close()
    ports.close.assert_awaited_once()
    database.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_worker_runtime_executes_shared_analysis_and_persists_report(monkeypatch, enabled):
    frozen, source, store = source_manifest(), source_detail(), MemoryReports()
    database = FakeDatabase()
    database.close = AsyncMock()
    ports = SimpleNamespace(inference=object(), prompts=object(), close=AsyncMock())
    config = SimpleNamespace(agent_scene=lambda scene: None,
                             inference=SimpleNamespace(model_routes={}))
    reader = SimpleNamespace(get_run_detail=AsyncMock(return_value=source))
    monkeypatch.setattr(worker_module, "load_model_runtime_config", lambda path: config)
    monkeypatch.setattr(worker_module, "build_model_runtime_ports", lambda *args, **kwargs: ports)
    monkeypatch.setattr(worker_module, "Database", lambda settings: database)
    monkeypatch.setattr(worker_module, "NativeStructuredAgentClient", lambda *args: object())
    monkeypatch.setattr(worker_module.NativeProductionBundleRuntimeRegistry, "from_json",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr(worker_module, "HotNewsQueryService", lambda **kwargs: reader)
    monkeypatch.setattr(worker_module, "S3DataLoopArtifactStore", lambda settings: store)
    monkeypatch.setattr(worker_module, "S3EvaluationDatasetArtifactStore", lambda settings: object())
    class Freezer:
        def __init__(self, **kwargs):
            pass

        async def load_manifest(self, *, tenant_id, dataset_id):
            assert (tenant_id, dataset_id) == (frozen.tenant_id, frozen.dataset_id)
            return frozen

    monkeypatch.setattr(handler_module, "EvaluationDatasetFreezer", Freezer)
    monkeypatch.setattr(handler_module, "PostgresEvaluationDatasetRepository", lambda session: object())
    settings = Settings(_env_file=None, **SETTINGS_BASE, environment="test",
                        conversation_data_analysis_enabled=False, data_loop_data_analysis_enabled=enabled)
    runtime = worker_module.create_data_loop_worker_runtime(settings)
    try:
        outcome = await DataLoopActivities(runtime.handler).run_data_loop_step(DataLoopStepCommand(
            tenant_id=frozen.tenant_id, run_idempotency_key="worker-analysis-test",
            step_type="analyze_dataset", step_key="analyze-dataset-v1",
            inputs={"dataset_id": str(frozen.dataset_id)},
        ))
        assert outcome.values["analysis_status"] == ("completed" if enabled else "disabled")
        receipt, report = next(iter(store.saved.values()))
        assert outcome.values["artifact_sha256"] == receipt.content_sha256
        if enabled:
            assert report["source_runs"][0]["results"]["overview"]["analysis"] == {
                "totals": {"impressions": 600, "clicks": 60, "total_duration_seconds": 840,
                           "effective_consumptions": 30, "interactions": 18},
                "weighted_ctr": "0.1",
            }
            reader.get_run_detail.assert_awaited_once_with(tenant_id=frozen.tenant_id, run_id=source.run.run_id)
        else:
            reader.get_run_detail.assert_not_awaited()
    finally:
        await runtime.close()
    ports.close.assert_awaited_once()
    database.close.assert_awaited_once()


def analysis_client(*, status="completed", permission=DataLoopPermission.READ):
    frozen = manifest("fresh_bad_case", DATASET_ID).model_copy(update={"tenant_id": str(TENANT_ID)})
    report = report_for(frozen, status=status)
    snapshot = DataLoopWorkflowSnapshot(
        tenant_id=str(TENANT_ID), phase="waiting_approval", dataset_id=str(DATASET_ID),
        evaluation_run_id="evaluation-1", gate_passed=True, waiting_for_approval=True,
        dataset_analysis_status=status, dataset_analysis_artifact_uri="s3://bucket/report.json",
        dataset_analysis_artifact_sha256="a" * 64, dataset_analysis_summary=report["summary"],
    )
    orchestrator = SimpleNamespace(get_snapshot=AsyncMock(return_value=snapshot))
    store = SimpleNamespace(get_json=AsyncMock(return_value=report))
    client = client_with(orchestrator, permission=permission)
    client.app.dependency_overrides[get_data_loop_artifact_store] = lambda: store
    return client, orchestrator, store, report


def test_report_api_reads_only_authorized_snapshot_reference():
    client, orchestrator, store, report = analysis_client()
    response = client.get("/api/v1/data-loop/runs/workflow-1/dataset-analysis")
    assert response.status_code == 200 and response.json() == report
    orchestrator.get_snapshot.assert_awaited_once_with("workflow-1", tenant_id=str(TENANT_ID))
    store.get_json.assert_awaited_once_with(storage_uri="s3://bucket/report.json", expected_sha256="a" * 64)
    snapshot = client.get("/api/v1/data-loop/runs/workflow-1").json()
    assert snapshot["dataset_analysis_summary"] == report["summary"]


@pytest.mark.parametrize("field,value", [("tenant_id", "other"), ("dataset_id", "other"),
                                         ("analysis_status", "disabled"), ("report_version", "future")])
def test_report_api_rejects_mixed_evidence(field, value):
    client, _, _, report = analysis_client()
    report[field] = value
    response = client.get("/api/v1/data-loop/runs/workflow-1/dataset-analysis")
    assert response.status_code == 503
    assert "source_runs" not in response.json()


def test_report_api_checksum_failure_is_unavailable():
    client, _, store, _ = analysis_client()
    store.get_json.side_effect = ArtifactIntegrityError("bad hash")
    assert client.get("/api/v1/data-loop/runs/workflow-1/dataset-analysis").status_code == 503


def test_report_api_requires_read_permission():
    client, orchestrator, store, _ = analysis_client(permission=DataLoopPermission.FEEDBACK_WRITE)
    assert client.get("/api/v1/data-loop/runs/workflow-1/dataset-analysis").status_code == 403
    orchestrator.get_snapshot.assert_not_awaited()
    store.get_json.assert_not_awaited()
