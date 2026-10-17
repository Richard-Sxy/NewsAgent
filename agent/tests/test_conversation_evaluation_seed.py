"""Simulation-only fixture guards, replay and transaction boundaries; no live DB."""

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import tools.seed_conversation_evaluation_demo as seed
from app.model_runtime.bundle_contract import UnsupportedProductionBundleRuntimeError
from app.model_runtime.core import PromptRegistry, PromptSpec
from app.model_runtime.hot_news import HOT_NEWS_SCENE
from app.models.analysis_feedback import AnalysisFeedbackCaseRecord, AnalysisFeedbackLabelRecord
from app.repositories.analysis_feedback import PostgresAnalysisFeedbackRepository
from app.repositories.evaluation_dataset import PostgresEvaluationDatasetRepository
from app.schemas.analysis_feedback import AnalysisFeedbackCase, AnalysisFeedbackLabel
from app.schemas.evaluation_dataset import EvaluationDatasetArtifactReceipt, EvaluationDatasetReference
from app.services.data_loop.feedback_collector import build_feedback_content_sha256
from tests.test_production_bundle import active_bundle


def local_config():
    registry = PromptRegistry((PromptSpec(HOT_NEWS_SCENE, "prompt-v1", "Local fixture prompt"),))
    return SimpleNamespace(inference=SimpleNamespace(provider="local", model_routes=("model-v1",)),
                           prompt_registry=lambda: registry)


def local_settings(base):
    return SimpleNamespace(environment="e2e", model_runtime_backend="native",
        database_url="postgresql+psycopg://newsagent_native:public-local-fixture@postgres:5432/newsagent_native",
        artifact_endpoint="http://minio:9000", artifact_bucket="newsagent-native-artifacts",
        hot_news_runtime_manifest_json=json.dumps([base.spec.model_dump()]))


class State:
    def __init__(self):
        self.cases, self.labels, self.datasets = {}, {}, {}
        self.active = 0
        self.events = []
        self.reviewed = []

    @asynccontextmanager
    async def session(self):
        self.active += 1
        self.events.append("begin")
        try:
            yield self
        finally:
            self.active -= 1
            self.events.append("end")


class Collector:
    """Typed stand-in; the actual application services have separate regressions."""
    def __init__(self, state):
        self.state = state
        self.submit_calls = 0
        self.corrupt_case = False
        self.corrupt_label = False

    async def collect(self, session, *, tenant_id, command, recorded_at, feedback_case_id):
        existing = self.state.cases.get(feedback_case_id)
        if existing is None:
            existing = AnalysisFeedbackCase(id=feedback_case_id, tenant_id=tenant_id, status="needs_label",
                recorded_at=recorded_at, content_sha256=build_feedback_content_sha256(command, tenant_id=tenant_id),
                **command.model_dump())
            self.state.cases[existing.id] = existing
        returned = existing.model_copy(update={"id": uuid4()}) if self.corrupt_case else existing
        return SimpleNamespace(case=returned, created=True)

    async def submit_label(self, session, *, tenant_id, command, recorded_at, label_id):
        self.submit_calls += 1
        existing = self.state.labels.get(label_id)
        if existing is None:
            existing = AnalysisFeedbackLabel(id=label_id, tenant_id=tenant_id, label_version=1,
                recorded_at=recorded_at, approval_status="pending",
                **command.model_dump(exclude={"expected_previous_version"}))
            self.state.labels[label_id] = existing
        returned = existing.model_copy(update={"labeled_by": "business-labeler"}) if self.corrupt_label else existing
        return SimpleNamespace(label=returned, created=True)

    async def approve_label(self, session, *, tenant_id, command):
        assert command.approved_by != seed.LABELER
        assert command.feedback_case_id in {seed.fixture_id("case", layer) for layer in seed.LAYERS}
        self.state.reviewed.append(command.feedback_case_id)
        label = self.state.labels[command.label_id].model_copy(update={"approval_status": "approved",
            "approved_by": command.approved_by, "approved_at": command.approved_at,
            "approval_idempotency_key": command.idempotency_key, "reviewed_by": command.approved_by,
            "reviewed_at": command.approved_at, "review_reason": command.review_reason,
            "review_idempotency_key": command.idempotency_key})
        self.state.labels[label.id] = label
        case = self.state.cases[command.feedback_case_id]
        self.state.cases[case.id] = case.model_copy(update={"status": "labeled"})
        return SimpleNamespace(label=label, created=True)


class DatasetRepository:
    def __init__(self, state):
        self.state = state

    async def get_by_idempotency_key(self, *, tenant_id, idempotency_key):
        return self.state.datasets.get((tenant_id, idempotency_key))

    async def get_by_name_version(self, *, tenant_id, dataset_name, dataset_version):
        return next((item for item in self.state.datasets.values() if item.tenant_id == tenant_id
            and item.dataset_name == dataset_name and item.dataset_version == dataset_version), None)

    async def list_approved_feedback_snapshots(self, *, tenant_id, feedback_case_ids, source_cutoff_at):
        result = []
        for case_id in feedback_case_ids:
            case = self.state.cases[case_id]
            label = next(item for item in self.state.labels.values() if item.feedback_case_id == case_id)
            assert label.approval_status == "approved"
            result.append(PostgresEvaluationDatasetRepository._to_approved_snapshot(
                AnalysisFeedbackCaseRecord(**PostgresAnalysisFeedbackRepository.to_case_values(case)),
                AnalysisFeedbackLabelRecord(**PostgresAnalysisFeedbackRepository.to_label_values(label))))
        return tuple(result)

    async def save_frozen_dataset(self, *, manifest, artifact, idempotency_key, request_fingerprint):
        assert self.state.active == 1
        reference = EvaluationDatasetReference(dataset_id=manifest.dataset_id, tenant_id=manifest.tenant_id,
            dataset_name=manifest.dataset_name, dataset_version=manifest.dataset_version,
            dataset_layer=manifest.dataset_layer, status="frozen", schema_version=manifest.schema_version,
            description=manifest.description, source_cutoff_at=manifest.source_cutoff_at, frozen_at=manifest.frozen_at,
            frozen_by=manifest.frozen_by, idempotency_key=idempotency_key, request_fingerprint=request_fingerprint,
            content_sha256=artifact.content_sha256, artifact_uri=artifact.storage_uri,
            artifact_size=artifact.content_size, case_count=len(manifest.cases))
        self.state.datasets[reference.tenant_id, idempotency_key] = reference
        return reference, True


class Artifacts:
    def __init__(self, state):
        self.state = state
        self.payloads = []

    async def put_json(self, **kwargs):
        assert self.state.active == 0, "The fixture must release DB transactions before MinIO upload"
        self.state.events.append("upload")
        self.payloads.append(json.loads(kwargs["content"]))
        return EvaluationDatasetArtifactReceipt(storage_uri=f"s3://fixture/{kwargs['dataset_id']}.json",
            content_sha256=kwargs["content_sha256"], content_size=len(kwargs["content"]))


def harness(monkeypatch):
    base = active_bundle(tenant_id=seed.TENANT)
    state = State()
    collector, artifacts = Collector(state), Artifacts(state)
    bundle_repository = SimpleNamespace(get_active_bundle=AsyncMock(return_value=base))
    monkeypatch.setattr(seed, "PostgresProductionBundleRepository", lambda session: bundle_repository)
    options = dict(settings=local_settings(base), model_config=local_config(), environ={"LOCAL_SIMULATION_ENABLED": "1"},
        database=state, collector=collector, artifacts=artifacts, repository_factory=DatasetRepository)
    return options, state, collector, artifacts, base


@pytest.mark.parametrize("changes,environment", [
    ({"environment": "production"}, {"LOCAL_SIMULATION_ENABLED": "1"}),
    ({}, {}), ({}, {"LOCAL_SIMULATION_ENABLED": "true"}),
    ({"database_url": "postgresql://newsagent_native:SECRET@enterprise.example:5432/newsagent_native"}, {"LOCAL_SIMULATION_ENABLED": "1"}),
    ({"database_url": "postgresql://newsagent_native:SECRET@postgres:5432/business"}, {"LOCAL_SIMULATION_ENABLED": "1"}),
    ({"artifact_endpoint": "https://enterprise.example"}, {"LOCAL_SIMULATION_ENABLED": "1"}),
    ({"artifact_bucket": "business-artifacts"}, {"LOCAL_SIMULATION_ENABLED": "1"}),
])
def test_seed_refuses_non_isolated_targets_without_exposing_sensitive_values(changes, environment):
    settings = local_settings(active_bundle(tenant_id=seed.TENANT))
    for key, value in changes.items():
        setattr(settings, key, value)
    with pytest.raises(seed.SeedGuardError) as refused:
        seed.check_target(settings, local_config(), environ=environment)
    assert "SECRET" not in str(refused.value) and "postgresql" not in str(refused.value)


def test_seed_refuses_enterprise_model_even_when_other_local_flags_are_set():
    settings = local_settings(active_bundle(tenant_id=seed.TENANT))
    config = local_config()
    config.inference.provider = "openai_compatible"
    with pytest.raises(seed.SeedGuardError, match="local_model_provider_required"):
        seed.check_target(settings, config, environ={"LOCAL_SIMULATION_ENABLED": "1"})


@pytest.mark.asyncio
async def test_check_reports_unpersisted_simulation_plan_and_performs_no_writes(monkeypatch):
    options, state, collector, artifacts, _ = harness(monkeypatch)
    report = await seed.seed_fixture(**options)
    assert report["mode"] == "check" and report["simulation_only"] is True
    assert report["human_review"] is report["enterprise_quality_validated"] is False
    assert report["analysis_status"] == "degraded"
    assert all(item["persisted"] is False for item in report["datasets"])
    assert not state.cases and not state.labels and not state.datasets and not artifacts.payloads
    assert collector.submit_calls == 0


@pytest.mark.asyncio
async def test_apply_creates_only_owned_three_layer_synthetic_fixtures_and_replays(monkeypatch):
    options, state, _, artifacts, base = harness(monkeypatch)
    first = await seed.seed_fixture(**options, apply=True)
    repeat = await seed.seed_fixture(**options, apply=True)
    assert {item["layer"] for item in first["datasets"]} == set(seed.LAYERS)
    assert len(state.cases) == len(state.labels) == len(state.datasets) == len(artifacts.payloads) == 3
    assert [item["dataset_id"] for item in first["datasets"]] == [item["dataset_id"] for item in repeat["datasets"]]
    assert all(item["created"] is False for item in repeat["datasets"])
    assert all(item["persisted"] and len(item["content_sha256"]) == 64 for item in first["datasets"])
    assert first["human_review"] is first["enterprise_quality_validated"] is False
    assert set(state.reviewed) == {seed.fixture_id("case", layer) for layer in seed.LAYERS}
    assert all(item.labeled_by == seed.LABELER and item.approved_by == seed.REVIEWER for item in state.labels.values())
    assert seed.LABELER != seed.REVIEWER
    assert all(item.run_id is None and item.source_reference.diagnostic_code == "synthetic_fixture" for item in state.cases.values())
    assert base.status == "active" and first["active_bundle_version"] == base.bundle_version
    for manifest in artifacts.payloads:
        assert manifest["description"] == seed.SIMULATION_NOTICE
        assert manifest["cases"][0]["expected"]["allowed_dominant_drivers"] == ["consumption"]
        assert manifest["cases"][0]["lineage"]["run_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["case", "label"])
async def test_seed_never_reviews_foreign_case_ids_or_non_fixture_label_authors(monkeypatch, corruption):
    options, state, collector, artifacts, _ = harness(monkeypatch)
    if corruption == "case":
        collector.corrupt_case = True
    else:
        collector.corrupt_label = True
    with pytest.raises(seed.SeedGuardError, match="refusing_to_review_non_fixture"):
        await seed.seed_fixture(**options, apply=True)
    assert not state.reviewed and not state.datasets and not artifacts.payloads
    if corruption == "case":
        assert collector.submit_calls == 0


@pytest.mark.asyncio
async def test_seed_validates_active_bundle_runtime_before_any_fixture_mutation(monkeypatch):
    options, state, _, artifacts, base = harness(monkeypatch)
    options["settings"].hot_news_runtime_manifest_json = "[]"
    with pytest.raises(UnsupportedProductionBundleRuntimeError):
        await seed.seed_fixture(**options, apply=True)
    assert not state.cases and not state.labels and not artifacts.payloads
    assert base.status == "active"

