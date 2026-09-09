import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.repositories.evaluation_dataset import EvaluationDatasetConflictError
from app.schemas.evaluation_dataset import (
    ApprovedFeedbackSnapshot,
    EvaluationDatasetArtifactReceipt,
    EvaluationDatasetCaseIndex,
    EvaluationDatasetReference,
    EvaluationExpectedLabel,
    EvaluationSourceLineage,
    FreezeEvaluationDatasetCommand,
)
from app.schemas.hot_news import (
    HotNewsAnalysisInput,
    HotNewsMetrics,
    HotScoreComponents,
    RelatedNewsEvidence,
)
from app.services.data_loop.dataset_freezer import (
    EvaluationDatasetFreezer,
    EvaluationDatasetIntegrityError,
    EvaluationDatasetNotReadyError,
    to_hot_news_evaluation_dataset,
)


CASE_A = UUID("00000000-0000-0000-0000-000000000001")
CASE_B = UUID("00000000-0000-0000-0000-000000000002")
DATASET_ID = UUID("20000000-0000-0000-0000-000000000001")
FROZEN_AT = datetime(2026, 9, 9, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 8, 23, tzinfo=UTC)


def _input(news_id: str, evidence_id: str) -> HotNewsAnalysisInput:
    return HotNewsAnalysisInput(
        news_id=news_id,
        title=f"热点 {news_id}",
        summary="可回放摘要",
        content_type="article",
        window_start=datetime(2026, 9, 8, tzinfo=UTC),
        window_end=datetime(2026, 9, 9, tzinfo=UTC),
        hot_score=0.75,
        metrics=HotNewsMetrics(
            impressions=100,
            clicks=20,
            ctr=0.2,
            unique_users=18,
            effective_consumptions=9,
            interactions=4,
        ),
        score_components=HotScoreComponents(
            click=0.7,
            consumption=0.4,
            interaction=0.2,
            growth=0.8,
        ),
        related_news=[
            RelatedNewsEvidence(
                news_id=evidence_id,
                title="可引用证据",
                final_score=0.8,
            )
        ],
        analysis_policy_version="prompt-v1",
    )


def _snapshot(case_id: UUID, suffix: str) -> ApprovedFeedbackSnapshot:
    news_id = f"news-{suffix}"
    evidence_id = f"evidence-{suffix}"
    return ApprovedFeedbackSnapshot(
        tenant_id="tenant-1",
        feedback_case_id=case_id,
        news_id=news_id,
        severity="high",
        analysis_input_snapshot=_input(news_id, evidence_id),
        analysis_output_snapshot={
            "news_id": news_id,
            "dominant_driver": "growth",
        },
        expected=EvaluationExpectedLabel(
            verdict="incorrect",
            allowed_dominant_drivers=("click",),
            required_evidence_news_ids=(evidence_id,),
            required_metric_keys=("clicks",),
            operator_comment="人工已确认主要驱动因素。",
        ),
        lineage=EvaluationSourceLineage(
            feedback_case_id=case_id,
            feedback_content_sha256=("a" if suffix == "a" else "b") * 64,
            run_id=None,
            run_idempotency_key=f"run-{suffix}",
            source_type="operator_corrected",
            problem_type="analysis_incorrect",
            source_reference={
                "reference_type": "operator_decision",
                "reference_id": f"decision-{suffix}",
            },
            production_bundle_version="bundle-v1",
            occurred_at=datetime(2026, 9, 8, 12, tzinfo=UTC),
            recorded_at=datetime(2026, 9, 8, 13, tzinfo=UTC),
            label_id=UUID(
                "10000000-0000-0000-0000-00000000000"
                + ("1" if suffix == "a" else "2")
            ),
            label_version=1,
            label_approved_by="reviewer-1",
            label_approved_at=datetime(2026, 9, 8, 14, tzinfo=UTC),
        ),
    )


def _command(ids=(CASE_B, CASE_A), **overrides):
    values = {
        "tenant_id": "tenant-1",
        "dataset_name": "daily-bad-cases",
        "dataset_version": "2026-09-09",
        "dataset_layer": "fresh_bad_case",
        "description": "日度人工审批 Bad Case",
        "feedback_case_ids": ids,
        "source_cutoff_at": CUTOFF,
        "frozen_by": "reviewer-1",
        "idempotency_key": "freeze-daily-20260909",
    }
    values.update(overrides)
    return FreezeEvaluationDatasetCommand(**values)


class FakeArtifactStore:
    def __init__(self):
        self.payloads = {}
        self.put_calls = 0
        self.get_calls = 0

    async def put_json(self, **kwargs):
        self.put_calls += 1
        content = kwargs["content"]
        digest = hashlib.sha256(content).hexdigest()
        uri = f"s3://tests/{kwargs['dataset_id']}/{digest}.json"
        self.payloads[uri] = json.loads(content)
        return EvaluationDatasetArtifactReceipt(
            storage_uri=uri,
            content_sha256=digest,
            content_size=len(content),
        )

    async def get_json(self, *, storage_uri, expected_sha256):
        self.get_calls += 1
        payload = self.payloads[storage_uri]
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        return payload


class FakeRepository:
    def __init__(self, snapshots):
        self.snapshots = {item.feedback_case_id: item for item in snapshots}
        self.references = {}
        self.saved_manifest = None

    async def get_by_idempotency_key(self, *, tenant_id, idempotency_key):
        return self.references.get((tenant_id, idempotency_key))

    async def get_by_id(self, *, tenant_id, dataset_id):
        for (stored_tenant, _), reference in self.references.items():
            if stored_tenant == tenant_id and reference.dataset_id == dataset_id:
                return reference
        return None

    async def get_by_name_version(
        self, *, tenant_id, dataset_name, dataset_version
    ):
        for (stored_tenant, _), reference in self.references.items():
            if (
                stored_tenant == tenant_id
                and reference.dataset_name == dataset_name
                and reference.dataset_version == dataset_version
            ):
                return reference
        return None

    async def list_case_indexes(self, **kwargs):
        if self.saved_manifest is None:
            return ()
        return tuple(
            EvaluationDatasetCaseIndex(
                dataset_id=self.saved_manifest.dataset_id,
                tenant_id=self.saved_manifest.tenant_id,
                feedback_case_id=case.feedback_case_id,
                position=position,
                case_id=case.case_id,
                news_id=case.news_id,
                dataset_layer=case.layer,
                severity=case.severity,
                label_version=case.lineage.label_version,
                case_content_sha256=case.content_sha256,
            )
            for position, case in enumerate(self.saved_manifest.cases)
        )

    async def list_approved_feedback_snapshots(
        self, *, tenant_id, feedback_case_ids, source_cutoff_at
    ):
        return tuple(
            self.snapshots[item]
            for item in feedback_case_ids
            if item in self.snapshots
            and self.snapshots[item].tenant_id == tenant_id
            and self.snapshots[item].lineage.recorded_at <= source_cutoff_at
        )

    async def save_frozen_dataset(
        self,
        *,
        manifest,
        artifact,
        idempotency_key,
        request_fingerprint,
    ):
        key = (manifest.tenant_id, idempotency_key)
        existing = self.references.get(key)
        if existing is not None:
            if existing.request_fingerprint != request_fingerprint:
                raise EvaluationDatasetConflictError("different request")
            return existing, False
        self.saved_manifest = manifest
        reference = EvaluationDatasetReference(
            dataset_id=manifest.dataset_id,
            tenant_id=manifest.tenant_id,
            dataset_name=manifest.dataset_name,
            dataset_version=manifest.dataset_version,
            dataset_layer=manifest.dataset_layer,
            status="frozen",
            schema_version=manifest.schema_version,
            description=manifest.description,
            source_cutoff_at=manifest.source_cutoff_at,
            frozen_at=manifest.frozen_at,
            frozen_by=manifest.frozen_by,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            content_sha256=manifest.content_sha256,
            artifact_uri=artifact.storage_uri,
            artifact_size=artifact.content_size,
            case_count=len(manifest.cases),
        )
        self.references[key] = reference
        return reference, True


def _freezer(repo, store):
    return EvaluationDatasetFreezer(
        repository=repo,
        artifact_store=store,
        clock=lambda: FROZEN_AT,
        id_factory=lambda _: DATASET_ID,
    )


@pytest.mark.asyncio
async def test_freezes_approved_cases_in_canonical_order_and_is_idempotent():
    repo = FakeRepository([_snapshot(CASE_A, "a"), _snapshot(CASE_B, "b")])
    store = FakeArtifactStore()
    freezer = _freezer(repo, store)
    command = _command()

    first = await freezer.freeze(command)
    second = await freezer.freeze(command)

    assert first.created is True
    assert second.created is False
    assert first.dataset == second.dataset
    assert store.put_calls == 1
    assert [case.feedback_case_id for case in repo.saved_manifest.cases] == [
        CASE_A,
        CASE_B,
    ]
    assert first.dataset.dataset_layer == "fresh_bad_case"
    assert first.dataset.content_sha256 == repo.saved_manifest.content_sha256

    loaded = await freezer.load_manifest(
        tenant_id="tenant-1",
        dataset_id=DATASET_ID,
    )
    assert loaded == repo.saved_manifest

    replay_dataset = to_hot_news_evaluation_dataset(loaded)
    assert replay_dataset.dataset_name == "daily-bad-cases"
    assert [case.case_id for case in replay_dataset.cases] == [
        f"feedback:{CASE_A}",
        f"feedback:{CASE_B}",
    ]
    assert replay_dataset.cases[0].expected.required_metric_keys == (
        "clicks",
    )


@pytest.mark.asyncio
async def test_missing_or_unapproved_feedback_stops_before_artifact_write():
    repo = FakeRepository([_snapshot(CASE_A, "a")])
    store = FakeArtifactStore()

    with pytest.raises(EvaluationDatasetNotReadyError, match="not approved"):
        await _freezer(repo, store).freeze(_command())

    assert store.put_calls == 0


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_command_is_rejected():
    repo = FakeRepository([_snapshot(CASE_A, "a"), _snapshot(CASE_B, "b")])
    store = FakeArtifactStore()
    freezer = _freezer(repo, store)
    await freezer.freeze(_command())

    with pytest.raises(EvaluationDatasetConflictError):
        await freezer.freeze(_command(description="不同的冻结内容"))

    assert store.put_calls == 1


@pytest.mark.asyncio
async def test_manifest_load_rejects_database_artifact_identity_mismatch():
    repo = FakeRepository([_snapshot(CASE_A, "a"), _snapshot(CASE_B, "b")])
    store = FakeArtifactStore()
    freezer = _freezer(repo, store)
    result = await freezer.freeze(_command())
    payload = store.payloads[result.dataset.artifact_uri]
    payload["dataset_version"] = "tampered"

    with pytest.raises((AssertionError, EvaluationDatasetIntegrityError)):
        await freezer.load_manifest(
            tenant_id="tenant-1",
            dataset_id=DATASET_ID,
        )


@pytest.mark.asyncio
async def test_manifest_load_rejects_cross_tenant_repository_result_before_io():
    repo = FakeRepository([_snapshot(CASE_A, "a"), _snapshot(CASE_B, "b")])
    store = FakeArtifactStore()
    freezer = _freezer(repo, store)
    result = await freezer.freeze(_command())
    reference = result.dataset.model_copy(update={"tenant_id": "tenant-2"})

    async def malicious_get_by_id(**kwargs):
        return reference

    repo.get_by_id = malicious_get_by_id
    get_calls_before = store.get_calls
    with pytest.raises(EvaluationDatasetNotReadyError, match="not found"):
        await freezer.load_manifest(
            tenant_id="tenant-1",
            dataset_id=DATASET_ID,
        )
    assert store.get_calls == get_calls_before
