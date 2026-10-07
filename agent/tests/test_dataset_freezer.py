import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.evaluation_dataset import (
    EvaluationDatasetConflictError,
    PostgresEvaluationDatasetRepository,
)
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
        self.snapshots = {}
        for item in snapshots:
            self.snapshots.setdefault(item.feedback_case_id, []).append(item)
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
        selected = []
        for feedback_case_id in feedback_case_ids:
            eligible = [
                snapshot
                for snapshot in self.snapshots.get(feedback_case_id, ())
                if snapshot.tenant_id == tenant_id
                and snapshot.lineage.recorded_at <= source_cutoff_at
                and snapshot.lineage.label_approved_at <= source_cutoff_at
            ]
            if eligible:
                selected.append(
                    max(
                        eligible,
                        key=lambda snapshot: snapshot.lineage.label_version,
                    )
                )
        return tuple(selected)

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


async def _end_read_transaction() -> None:
    return None


async def _freeze(freezer, command):
    return await freezer.freeze(
        command,
        end_prepare_transaction=_end_read_transaction,
    )


@pytest.mark.asyncio
async def test_freezes_approved_cases_in_canonical_order_and_is_idempotent():
    repo = FakeRepository([_snapshot(CASE_A, "a"), _snapshot(CASE_B, "b")])
    store = FakeArtifactStore()
    freezer = _freezer(repo, store)
    command = _command()

    first = await _freeze(freezer, command)
    second = await _freeze(freezer, command)

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
        await _freeze(_freezer(repo, store), _command())

    assert store.put_calls == 0


@pytest.mark.asyncio
async def test_cutoff_uses_latest_label_approved_at_that_point_in_time():
    original = _snapshot(CASE_A, "a")
    approved_after_cutoff = original.model_copy(
        update={
            "lineage": original.lineage.model_copy(
                update={
                    "label_id": UUID(
                        "10000000-0000-0000-0000-000000000099"
                    ),
                    "label_version": 2,
                    "label_approved_at": datetime(
                        2026, 9, 9, 0, tzinfo=UTC
                    ),
                }
            )
        }
    )
    repo = FakeRepository((original, approved_after_cutoff))
    store = FakeArtifactStore()

    result = await _freeze(
        _freezer(repo, store),
        _command(ids=(CASE_A,)),
    )

    assert result.created is True
    assert repo.saved_manifest.cases[0].lineage.label_version == 1
    assert (
        repo.saved_manifest.cases[0].lineage.label_approved_at
        <= repo.saved_manifest.source_cutoff_at
    )


@pytest.mark.asyncio
async def test_freeze_ends_read_transaction_before_artifact_upload():
    events = []

    class OrderingStore(FakeArtifactStore):
        async def put_json(self, **kwargs):
            assert events == ["read-transaction-ended"]
            events.append("artifact-uploaded")
            return await super().put_json(**kwargs)

    async def end_read_transaction():
        events.append("read-transaction-ended")

    repo = FakeRepository((_snapshot(CASE_A, "a"),))
    freezer = _freezer(repo, OrderingStore())
    await freezer.freeze(
        _command(ids=(CASE_A,)),
        end_prepare_transaction=end_read_transaction,
    )

    assert events == ["read-transaction-ended", "artifact-uploaded"]


@pytest.mark.asyncio
async def test_retry_uses_same_manifest_hash_when_wall_clock_advances():
    repo = FakeRepository((_snapshot(CASE_A, "a"),))
    store = FakeArtifactStore()
    command = _command(ids=(CASE_A,))
    first_freezer = EvaluationDatasetFreezer(
        repository=repo,
        artifact_store=store,
        clock=lambda: FROZEN_AT,
        id_factory=lambda _: DATASET_ID,
    )
    retry_freezer = EvaluationDatasetFreezer(
        repository=repo,
        artifact_store=store,
        clock=lambda: datetime(2026, 9, 10, 1, tzinfo=UTC),
        id_factory=lambda _: DATASET_ID,
    )

    first = await first_freezer.prepare(command)
    retry = await retry_freezer.prepare(command)
    first_receipt = await first_freezer.upload(first)
    retry_receipt = await retry_freezer.upload(retry)

    assert first.manifest.frozen_at == CUTOFF
    assert retry.manifest.frozen_at == CUTOFF
    assert first.content_sha256 == retry.content_sha256
    assert first_receipt.storage_uri == retry_receipt.storage_uri
    assert len(store.payloads) == 1


@pytest.mark.asyncio
async def test_repository_queries_case_and_label_as_of_same_cutoff():
    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class CapturingSession:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return EmptyResult()

    session = CapturingSession()
    repository = PostgresEvaluationDatasetRepository(session)
    await repository.list_approved_case_ids_for_window(
        tenant_id="tenant-1",
        occurred_start=datetime(2026, 9, 8, tzinfo=UTC),
        occurred_end=datetime(2026, 9, 9, tzinfo=UTC),
        source_cutoff_at=CUTOFF,
        limit=10,
    )

    compiled = session.statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "feedback_cases.recorded_at <=" in sql
    assert sql.count("feedback_labels.approved_at <=") == 2
    assert "max(feedback_labels.label_version)" in sql
    assert "feedback_cases.status" not in sql
    parameter_values = list(compiled.params.values())
    assert "tenant-1" in parameter_values
    assert CUTOFF in parameter_values
    assert ["approved", "superseded"] in parameter_values


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_command_is_rejected():
    repo = FakeRepository([_snapshot(CASE_A, "a"), _snapshot(CASE_B, "b")])
    store = FakeArtifactStore()
    freezer = _freezer(repo, store)
    await _freeze(freezer, _command())

    with pytest.raises(EvaluationDatasetConflictError):
        await _freeze(freezer, _command(description="不同的冻结内容"))

    assert store.put_calls == 1


@pytest.mark.asyncio
async def test_manifest_load_rejects_database_artifact_identity_mismatch():
    repo = FakeRepository([_snapshot(CASE_A, "a"), _snapshot(CASE_B, "b")])
    store = FakeArtifactStore()
    freezer = _freezer(repo, store)
    result = await _freeze(freezer, _command())
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
    result = await _freeze(freezer, _command())
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
