from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_data_loop_orchestrator,
    get_tenant_id,
    get_user_id,
)
from app.main import create_app
from app.workflows.data_loop_contracts import DataLoopWorkflowSnapshot


def client_with(orchestrator):
    app = create_app()
    app.dependency_overrides[get_data_loop_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_tenant_id] = lambda: uuid4()
    app.dependency_overrides[get_user_id] = lambda: uuid4()
    return TestClient(app)


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

    response = client_with(orchestrator).post(
        "/api/v1/data-loop/runs",
        json=start_payload(candidate_id),
    )

    assert response.status_code == 202
    assert response.json() == {"workflow_id": "workflow-1", "status": "accepted"}
    command = orchestrator.start.await_args.args[0]
    assert command.candidate_id == str(candidate_id)
    assert command.tenant_id


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

    response = client_with(orchestrator).get("/api/v1/data-loop/runs/workflow-1")

    assert response.status_code == 200
    assert response.json()["waiting_for_approval"] is True


def test_submit_decision_uses_authenticated_operator() -> None:
    orchestrator = SimpleNamespace(submit_decision=AsyncMock())

    response = client_with(orchestrator).post(
        "/api/v1/data-loop/runs/workflow-1/decision",
        json={
            "action": "approve",
            "reason": "regression checks passed",
            "idempotency_key": "approval-request-1",
        },
    )

    assert response.status_code == 202
    decision = orchestrator.submit_decision.await_args.args[1]
    assert decision.actor_id
    assert decision.action == "approve"
