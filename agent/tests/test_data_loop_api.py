from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.dependencies import (
    DataLoopPermission,
    get_data_loop_orchestrator,
    get_data_loop_gateway_token,
)
from app.main import create_app
from app.services.data_loop.orchestrator import (
    DataLoopActivationRecoveryNotAllowedError,
    DataLoopDecisionNotAllowedError,
    DataLoopStartConflictError,
)
from app.workflows.data_loop_contracts import (
    DataLoopDecisionUpdateResult,
    DataLoopWorkflowSnapshot,
)


TENANT_ID = uuid4()
USER_ID = uuid4()
GATEWAY_TOKEN = "test-data-loop-gateway-token"


def client_with(
    orchestrator,
    *,
    permission: DataLoopPermission = DataLoopPermission.ADMIN,
    bearer_token: str = GATEWAY_TOKEN,
):
    app = create_app()
    app.dependency_overrides[get_data_loop_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_data_loop_gateway_token] = (
        lambda: GATEWAY_TOKEN
    )
    return TestClient(
        app,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "X-Tenant-ID": str(TENANT_ID),
            "X-User-ID": str(USER_ID),
            "X-Data-Loop-Roles": permission.value,
        },
    )


def start_payload(candidate_id):
    start = datetime(2026, 9, 8, tzinfo=timezone.utc)
    return {
        "window_start": start.isoformat(),
        "window_end": (start + timedelta(days=1)).isoformat(),
        "dataset_name": "daily-feedback",
        "dataset_version": "2026-09-08.v1",
        "golden_dataset_id": str(uuid4()),
        "high_risk_regression_dataset_id": str(uuid4()),
        "candidate_id": str(candidate_id),
        "evaluation_policy_version": "gate-v1",
        "idempotency_key": "data-loop-request-1",
    }


def test_start_data_loop_uses_authenticated_tenant() -> None:
    orchestrator = SimpleNamespace(start=AsyncMock(return_value="workflow-1"))
    candidate_id = uuid4()

    response = client_with(
        orchestrator,
        permission=DataLoopPermission.RUN,
    ).post(
        "/api/v1/data-loop/runs",
        json=start_payload(candidate_id),
    )

    assert response.status_code == 202
    assert response.json() == {"workflow_id": "workflow-1", "status": "accepted"}
    command = orchestrator.start.await_args.args[0]
    assert command.candidate_id == str(candidate_id)
    assert command.tenant_id == str(TENANT_ID)


def test_get_data_loop_snapshot() -> None:
    orchestrator = SimpleNamespace(
        get_snapshot=AsyncMock(
            return_value=DataLoopWorkflowSnapshot(
                tenant_id="tenant-1",
                phase="waiting_approval",
                dataset_id="dataset-1",
                evaluation_run_id="evaluation-1",
                gate_passed=True,
                waiting_for_approval=True,
            )
        )
    )

    response = client_with(
        orchestrator,
        permission=DataLoopPermission.READ,
    ).get("/api/v1/data-loop/runs/workflow-1")

    assert response.status_code == 200
    assert response.json()["waiting_for_approval"] is True


def test_submit_decision_uses_authenticated_operator() -> None:
    orchestrator = SimpleNamespace(
        submit_decision=AsyncMock(
            return_value=DataLoopDecisionUpdateResult(
                action="approve",
                idempotency_key="approval-request-1",
                created=True,
            )
        )
    )

    response = client_with(
        orchestrator,
        permission=DataLoopPermission.RELEASE_APPROVE,
    ).post(
        "/api/v1/data-loop/runs/workflow-1/decision",
        json={
            "action": "approve",
            "reason": "regression checks passed",
            "idempotency_key": "approval-request-1",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "workflow_id": "workflow-1",
        "status": "submitted",
        "created": True,
    }
    decision = orchestrator.submit_decision.await_args.args[1]
    assert decision.actor_id == str(USER_ID)
    assert decision.action == "approve"


def test_submit_second_decision_returns_conflict() -> None:
    orchestrator = SimpleNamespace(
        submit_decision=AsyncMock(
            side_effect=DataLoopDecisionNotAllowedError(
                "a promotion decision has already been submitted"
            )
        )
    )

    response = client_with(
        orchestrator,
        permission=DataLoopPermission.RELEASE_APPROVE,
    ).post(
        "/api/v1/data-loop/runs/workflow-1/decision",
        json={
            "action": "reject",
            "reason": "changed decision",
            "idempotency_key": "approval-request-2",
        },
    )

    assert response.status_code == 409
    assert "already" in response.json()["detail"]


def test_start_same_key_with_different_content_returns_conflict() -> None:
    orchestrator = SimpleNamespace(
        start=AsyncMock(
            side_effect=DataLoopStartConflictError(
                "Data Loop idempotency key is already bound to different content"
            )
        )
    )

    response = client_with(
        orchestrator,
        permission=DataLoopPermission.RUN,
    ).post(
        "/api/v1/data-loop/runs",
        json=start_payload(uuid4()),
    )

    assert response.status_code == 409
    assert "different content" in response.json()["detail"]


def test_recover_activation_uses_authenticated_context() -> None:
    orchestrator = SimpleNamespace(
        recover_activation=AsyncMock(return_value="recovery-workflow-1")
    )

    response = client_with(
        orchestrator,
        permission=DataLoopPermission.RELEASE_APPROVE,
    ).post(
        "/api/v1/data-loop/runs/workflow-1/activation/recover",
        json={"idempotency_key": "recovery-request-1"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "source_workflow_id": "workflow-1",
        "recovery_workflow_id": "recovery-workflow-1",
        "status": "accepted",
    }
    kwargs = orchestrator.recover_activation.await_args.kwargs
    assert kwargs["tenant_id"] == str(TENANT_ID)
    assert kwargs["requested_by"] == str(USER_ID)
    assert kwargs["idempotency_key"] == "recovery-request-1"


def test_recover_activation_returns_conflict_without_recoverable_approval() -> None:
    orchestrator = SimpleNamespace(
        recover_activation=AsyncMock(
            side_effect=DataLoopActivationRecoveryNotAllowedError(
                "Data Loop workflow has no approved activation to recover"
            )
        )
    )

    response = client_with(
        orchestrator,
        permission=DataLoopPermission.RELEASE_APPROVE,
    ).post(
        "/api/v1/data-loop/runs/workflow-1/activation/recover",
        json={"idempotency_key": "recovery-request-1"},
    )

    assert response.status_code == 409
    assert "no approved activation" in response.json()["detail"]


def test_data_loop_api_fails_closed_when_gateway_token_is_not_configured() -> None:
    app = create_app()
    app.dependency_overrides[get_data_loop_orchestrator] = lambda: SimpleNamespace()
    client = TestClient(
        app,
        headers={
            "Authorization": f"Bearer {GATEWAY_TOKEN}",
            "X-Tenant-ID": str(TENANT_ID),
            "X-User-ID": str(USER_ID),
            "X-Data-Loop-Roles": DataLoopPermission.READ.value,
        },
    )

    response = client.get("/api/v1/data-loop/runs/workflow-1")

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_data_loop_api_rejects_invalid_gateway_credential() -> None:
    orchestrator = SimpleNamespace()

    response = client_with(
        orchestrator,
        bearer_token="wrong-token",
    ).get("/api/v1/data-loop/runs/workflow-1")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_data_loop_api_requires_gateway_user_identity() -> None:
    app = create_app()
    app.dependency_overrides[get_data_loop_gateway_token] = (
        lambda: GATEWAY_TOKEN
    )
    app.dependency_overrides[get_data_loop_orchestrator] = (
        lambda: SimpleNamespace()
    )
    client = TestClient(
        app,
        headers={
            "Authorization": f"Bearer {GATEWAY_TOKEN}",
            "X-Tenant-ID": str(TENANT_ID),
            "X-Data-Loop-Roles": DataLoopPermission.READ.value,
        },
    )

    response = client.get("/api/v1/data-loop/runs/workflow-1")

    assert response.status_code == 401
    assert "identity headers" in response.json()["detail"]


def test_data_loop_api_requires_an_explicit_permission() -> None:
    app = create_app()
    app.dependency_overrides[get_data_loop_gateway_token] = (
        lambda: GATEWAY_TOKEN
    )
    app.dependency_overrides[get_data_loop_orchestrator] = (
        lambda: SimpleNamespace()
    )
    client = TestClient(
        app,
        headers={
            "Authorization": f"Bearer {GATEWAY_TOKEN}",
            "X-Tenant-ID": str(TENANT_ID),
            "X-User-ID": str(USER_ID),
        },
    )

    response = client.get("/api/v1/data-loop/runs/workflow-1")

    assert response.status_code == 403
    assert "permissions are required" in response.json()["detail"]


def test_read_permission_cannot_start_data_loop() -> None:
    orchestrator = SimpleNamespace(start=AsyncMock(return_value="workflow-1"))

    response = client_with(
        orchestrator,
        permission=DataLoopPermission.READ,
    ).post(
        "/api/v1/data-loop/runs",
        json=start_payload(uuid4()),
    )

    assert response.status_code == 403
    assert DataLoopPermission.RUN.value in response.json()["detail"]
    orchestrator.start.assert_not_awaited()


def test_run_permission_cannot_approve_release() -> None:
    orchestrator = SimpleNamespace(submit_decision=AsyncMock())

    response = client_with(
        orchestrator,
        permission=DataLoopPermission.RUN,
    ).post(
        "/api/v1/data-loop/runs/workflow-1/decision",
        json={
            "action": "approve",
            "reason": "should be forbidden",
            "idempotency_key": "unauthorized-approval-1",
        },
    )

    assert response.status_code == 403
    assert DataLoopPermission.RELEASE_APPROVE.value in response.json()["detail"]
    orchestrator.submit_decision.assert_not_awaited()
