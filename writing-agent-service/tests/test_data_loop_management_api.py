from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

import app.api.data_loop as data_loop_api_module
from app.api.dependencies import (
    DataLoopPermission,
    get_analysis_feedback_collector,
    get_data_loop_gateway_token,
    get_data_loop_runtime_registry,
    get_evaluation_dataset_artifact_store,
    get_hot_news_decision_service,
    get_production_bundle_service,
    get_session,
)
from app.main import create_app
from app.services.data_loop.feedback_collector import FeedbackLabelWriteOutcome
from app.services.production_bundle import CandidateWriteOutcome
from app.services.production_bundle_runtime import (
    UnsupportedProductionBundleRuntimeError,
)
from tests.test_feedback_collector import pending_label
from tests.test_dataset_freezer import (
    CASE_A,
    CUTOFF,
    FakeArtifactStore,
    FakeRepository,
    _snapshot,
)
from tests.test_production_bundle import (
    NOW,
    active_bundle,
    evaluation_command,
    proposal,
)
from app.services.production_bundle import ProductionBundleDomainService


TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
GATEWAY_TOKEN = "test-data-loop-gateway-token"


class AllowRuntimeRegistry:
    def ensure_supported(self, spec) -> None:
        return None


class RejectRuntimeRegistry:
    def ensure_supported(self, spec) -> None:
        raise UnsupportedProductionBundleRuntimeError(
            "production bundle snapshot is not registered"
        )


def client_with(
    dependencies,
    *,
    permission: DataLoopPermission = DataLoopPermission.ADMIN,
):
    app = create_app()
    app.dependency_overrides[get_data_loop_gateway_token] = (
        lambda: GATEWAY_TOKEN
    )
    app.dependency_overrides[get_data_loop_runtime_registry] = (
        AllowRuntimeRegistry
    )
    app.dependency_overrides[get_session] = lambda: object()
    for dependency, value in dependencies.items():
        def make_override(resolved):
            def override():
                return resolved

            return override

        app.dependency_overrides[dependency] = make_override(value)
    return TestClient(
        app,
        headers={
            "Authorization": f"Bearer {GATEWAY_TOKEN}",
            "X-Tenant-ID": str(TENANT_ID),
            "X-User-ID": str(USER_ID),
            "X-Data-Loop-Roles": permission.value,
        },
    )


def test_operator_rejection_uses_authenticated_actor_and_feedback_type() -> None:
    decision_id = uuid4()
    service = SimpleNamespace(
        record_decision=AsyncMock(
            return_value=(
                SimpleNamespace(
                    id=decision_id,
                    decision_type="rejected",
                ),
                True,
            )
        )
    )
    client = client_with(
        {get_hot_news_decision_service: service},
        permission=DataLoopPermission.FEEDBACK_WRITE,
    )

    response = client.post(
        "/api/v1/data-loop/operator-decisions",
        json={
            "run_id": str(uuid4()),
            "news_id": "news-1",
            "decision_type": "rejected",
            "reason": "缺少证据",
            "idempotency_key": "decision-request-1",
            "feedback_problem_type": "unsupported_claim",
            "feedback_severity": "high",
        },
    )

    assert response.status_code == 201
    call = service.record_decision.await_args.kwargs
    assert call["tenant_id"] == str(TENANT_ID)
    assert call["operator_id"] == str(USER_ID)
    assert call["feedback_problem_type"] == "unsupported_claim"
    assert call["feedback_severity"] == "high"


def test_submit_label_uses_server_side_reviewer_identity() -> None:
    label = pending_label().model_copy(
        update={"labeled_by": str(USER_ID), "labeled_at": NOW}
    )
    collector = SimpleNamespace(
        submit_label=AsyncMock(
            return_value=FeedbackLabelWriteOutcome(label=label, created=True)
        )
    )
    client = client_with(
        {get_analysis_feedback_collector: collector},
        permission=DataLoopPermission.LABEL_SUBMIT,
    )

    response = client.post(
        f"/api/v1/data-loop/feedback-cases/{label.feedback_case_id}/labels",
        json={
            "verdict": "incorrect",
            "allowed_dominant_drivers": ["click"],
            "required_metric_keys": ["clicks"],
            "operator_comment": "应当以点击指标为主",
            "idempotency_key": "feedback-label-api-1",
        },
    )

    assert response.status_code == 201
    command = collector.submit_label.await_args.kwargs["command"]
    assert command.labeled_by == str(USER_ID)
    assert command.feedback_case_id == label.feedback_case_id


def test_candidate_proposal_uses_authenticated_proposer() -> None:
    base = active_bundle(tenant_id=str(TENANT_ID))
    domain_command = proposal(
        base,
        tenant_id=str(TENANT_ID),
        proposed_by=str(USER_ID),
    )
    candidate = ProductionBundleDomainService().propose_candidate(
        base_bundle=base,
        command=domain_command,
        now=NOW,
    )
    service = SimpleNamespace(
        propose_candidate=AsyncMock(
            return_value=CandidateWriteOutcome(candidate, True)
        )
    )
    client = client_with(
        {get_production_bundle_service: service},
        permission=DataLoopPermission.CANDIDATE_MANAGE,
    )

    response = client.post(
        "/api/v1/data-loop/configuration-candidates",
        json={
            "base_bundle_id": str(base.id),
            "candidate_version": domain_command.candidate_version,
            "proposed_spec": domain_command.proposed_spec.model_dump(
                mode="json"
            ),
            "structured_diff": [
                item.model_dump(mode="json")
                for item in domain_command.structured_diff
            ],
            "proposal_reason": domain_command.proposal_reason,
            "idempotency_key": domain_command.idempotency_key,
        },
    )

    assert response.status_code == 201
    command = service.propose_candidate.await_args.kwargs["command"]
    assert command.tenant_id == str(TENANT_ID)
    assert command.proposed_by == str(USER_ID)


def test_evaluation_detail_exposes_three_cohorts_and_gate_failures(
    monkeypatch,
) -> None:
    domain = ProductionBundleDomainService()
    base = active_bundle(tenant_id=str(TENANT_ID))
    candidate = domain.propose_candidate(
        base_bundle=base,
        command=proposal(
            base,
            tenant_id=str(TENANT_ID),
            proposed_by="proposer-1",
        ),
        now=NOW,
    )
    evaluated = domain.record_evaluation(
        candidate=candidate,
        command=evaluation_command(candidate),
        now=NOW,
    )

    class EvaluationRepository:
        def __init__(self, session):
            pass

        async def get_evaluation(self, **kwargs):
            assert kwargs["tenant_id"] == str(TENANT_ID)
            return evaluated.evaluation_run

    monkeypatch.setattr(
        data_loop_api_module,
        "PostgresProductionBundleRepository",
        EvaluationRepository,
    )
    client = client_with({}, permission=DataLoopPermission.READ)

    response = client.get(
        f"/api/v1/data-loop/evaluation-runs/{evaluated.evaluation_run.id}"
    )

    assert response.status_code == 200
    payload = response.json()["evaluation"]
    assert set(payload["suite_metrics"]) == {
        "golden",
        "fresh_bad_case",
        "high_risk_regression",
    }
    assert payload["gate_decision"]["passed"] is True


def test_dataset_freeze_api_ends_read_transaction_before_upload(
    monkeypatch,
) -> None:
    session = SimpleNamespace(rollback=AsyncMock())
    snapshot = _snapshot(CASE_A, "a").model_copy(
        update={"tenant_id": str(TENANT_ID)}
    )
    repository = FakeRepository((snapshot,))

    class OrderingStore(FakeArtifactStore):
        async def put_json(self, **kwargs):
            session.rollback.assert_awaited_once()
            return await super().put_json(**kwargs)

    store = OrderingStore()
    monkeypatch.setattr(
        data_loop_api_module,
        "PostgresEvaluationDatasetRepository",
        lambda current_session: repository,
    )
    client = client_with(
        {
            get_session: session,
            get_evaluation_dataset_artifact_store: store,
        },
        permission=DataLoopPermission.DATASET_MANAGE,
    )

    response = client.post(
        "/api/v1/data-loop/datasets/freeze",
        json={
            "dataset_name": "manual-bad-cases",
            "dataset_version": "2026-09-08.v1",
            "dataset_layer": "fresh_bad_case",
            "description": "人工冻结的已审批 bad case",
            "feedback_case_ids": [str(CASE_A)],
            "source_cutoff_at": CUTOFF.isoformat(),
            "idempotency_key": "manual-freeze-api-1",
        },
    )

    assert response.status_code == 201
    assert response.json()["created"] is True
    assert repository.saved_manifest.tenant_id == str(TENANT_ID)
    assert repository.saved_manifest.frozen_by == str(USER_ID)
    session.rollback.assert_awaited_once()


def test_label_submitter_cannot_approve_a_label() -> None:
    collector = SimpleNamespace(approve_label=AsyncMock())
    client = client_with(
        {get_analysis_feedback_collector: collector},
        permission=DataLoopPermission.LABEL_SUBMIT,
    )

    response = client.post(
        f"/api/v1/data-loop/feedback-cases/{uuid4()}/labels/{uuid4()}/approve",
        json={
            "expected_label_version": 1,
            "idempotency_key": "separation-of-duties-1",
        },
    )

    assert response.status_code == 403
    assert DataLoopPermission.LABEL_APPROVE.value in response.json()["detail"]
    collector.approve_label.assert_not_awaited()


def test_label_reviewer_can_request_changes_with_server_identity() -> None:
    label = pending_label()
    rejected = label.model_copy(
        update={
            "approval_status": "rejected",
            "reviewed_by": str(USER_ID),
            "reviewed_at": label.labeled_at,
            "review_reason": "补充证据后重新提交",
            "review_idempotency_key": "label-review-api-1",
        }
    )
    collector = SimpleNamespace(
        request_label_changes=AsyncMock(
            return_value=FeedbackLabelWriteOutcome(rejected, True)
        )
    )
    client = client_with(
        {get_analysis_feedback_collector: collector},
        permission=DataLoopPermission.LABEL_APPROVE,
    )

    response = client.post(
        f"/api/v1/data-loop/feedback-cases/{label.feedback_case_id}"
        f"/labels/{label.id}/review",
        json={
            "action": "request_changes",
            "expected_label_version": 1,
            "reason": "补充证据后重新提交",
            "idempotency_key": "label-review-api-1",
        },
    )

    assert response.status_code == 200
    command = collector.request_label_changes.await_args.kwargs["command"]
    assert command.reviewed_by == str(USER_ID)
    assert command.review_reason == "补充证据后重新提交"
    assert response.json()["label"]["approval_status"] == "rejected"


def test_bootstrap_rejects_bundle_missing_from_runtime_manifest() -> None:
    base = active_bundle(tenant_id=str(TENANT_ID))
    service = SimpleNamespace(bootstrap_initial_bundle=AsyncMock())
    client = client_with(
        {
            get_production_bundle_service: service,
            get_data_loop_runtime_registry: RejectRuntimeRegistry(),
        },
        permission=DataLoopPermission.CANDIDATE_MANAGE,
    )

    response = client.post(
        "/api/v1/data-loop/production-bundles/bootstrap",
        json={
            "bundle_version": base.bundle_version,
            "spec": base.spec.model_dump(mode="json"),
        },
    )

    assert response.status_code == 409
    assert "not registered" in response.json()["detail"]
    service.bootstrap_initial_bundle.assert_not_awaited()


def test_rollback_rejects_target_missing_from_runtime_manifest(
    monkeypatch,
) -> None:
    target = active_bundle(
        tenant_id=str(TENANT_ID),
        status="inactive",
        deactivated_at=NOW,
    )

    class TargetRepository:
        async def get_bundle(self, **kwargs):
            assert kwargs == {
                "tenant_id": str(TENANT_ID),
                "bundle_id": target.id,
            }
            return target

    monkeypatch.setattr(
        data_loop_api_module,
        "PostgresProductionBundleRepository",
        lambda session: TargetRepository(),
    )
    service = SimpleNamespace(rollback_bundle=AsyncMock())
    client = client_with(
        {
            get_production_bundle_service: service,
            get_data_loop_runtime_registry: RejectRuntimeRegistry(),
        },
        permission=DataLoopPermission.ROLLBACK,
    )

    response = client.post(
        "/api/v1/data-loop/production-bundles/rollback",
        json={
            "target_bundle_id": str(target.id),
            "reason": "return to the last trusted runtime",
            "idempotency_key": "rollback-runtime-check-1",
        },
    )

    assert response.status_code == 409
    assert "not registered" in response.json()["detail"]
    service.rollback_bundle.assert_not_awaited()
