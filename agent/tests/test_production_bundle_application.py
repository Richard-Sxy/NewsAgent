from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.production_bundle as bundle_module
from app.schemas.production_bundle import (
    ActivateCandidateCommand,
    ApproveCandidateCommand,
)
from app.services.production_bundle import (
    ProductionBundleApplicationService,
    ProductionBundleConflictError,
    ProductionBundleDomainService,
)
from tests.test_production_bundle import (
    NOW,
    active_bundle,
    evaluation_command,
    proposal,
)


def install_repository(monkeypatch, **methods):
    defaults = {
        "get_candidate_by_idempotency_key": AsyncMock(return_value=None),
        "get_evaluation_by_idempotency_key": AsyncMock(return_value=None),
        "get_decision_by_idempotency_key": AsyncMock(return_value=None),
        "get_bundle_for_update": AsyncMock(return_value=None),
        "get_active_bundle_for_update": AsyncMock(return_value=None),
        "get_candidate_for_update": AsyncMock(return_value=None),
        "get_latest_evaluated_candidate_for_update": AsyncMock(
            return_value=None
        ),
        "get_evaluation_for_update": AsyncMock(return_value=None),
        "get_frozen_dataset_layers_for_update": AsyncMock(return_value={}),
        "insert_candidate": AsyncMock(return_value=True),
        "insert_evaluation": AsyncMock(return_value=True),
        "insert_bundle": AsyncMock(),
        "insert_decision": AsyncMock(return_value=True),
        "update_candidate": AsyncMock(return_value=True),
        "update_bundle_lifecycle": AsyncMock(return_value=True),
    }
    defaults.update(methods)
    repository = SimpleNamespace(**defaults)
    monkeypatch.setattr(
        bundle_module,
        "PostgresProductionBundleRepository",
        lambda session: repository,
    )
    return repository


@pytest.mark.asyncio
async def test_application_proposes_candidate_with_idempotency_fingerprint(
    monkeypatch,
) -> None:
    base = active_bundle()
    command = proposal(base)
    repository = install_repository(
        monkeypatch,
        get_bundle_for_update=AsyncMock(return_value=base),
    )

    outcome = await ProductionBundleApplicationService().propose_candidate(
        object(), command=command, now=NOW
    )

    assert outcome.created is True
    assert outcome.candidate.status == "pending_evaluation"
    call = repository.insert_candidate.await_args
    assert len(call.kwargs["request_fingerprint"]) == 64


@pytest.mark.asyncio
async def test_application_rejects_changed_idempotent_proposal(
    monkeypatch,
) -> None:
    base = active_bundle()
    command = proposal(base)
    candidate = ProductionBundleDomainService().propose_candidate(
        base_bundle=base,
        command=command,
        now=NOW,
    )
    install_repository(
        monkeypatch,
        get_candidate_by_idempotency_key=AsyncMock(
            return_value=(candidate, "f" * 64)
        ),
    )

    with pytest.raises(ProductionBundleConflictError):
        await ProductionBundleApplicationService().propose_candidate(
            object(), command=command, now=NOW
        )


@pytest.mark.asyncio
async def test_application_records_evaluation_only_for_three_frozen_layers(
    monkeypatch,
) -> None:
    base = active_bundle()
    domain = ProductionBundleDomainService()
    candidate = domain.propose_candidate(
        base_bundle=base,
        command=proposal(base),
        now=NOW,
    )
    command = evaluation_command(candidate)
    expected_layers = {
        command.suite_metrics.golden.dataset_id: "golden",
        command.suite_metrics.fresh_bad_case.dataset_id: "fresh_bad_case",
        command.suite_metrics.high_risk_regression.dataset_id: (
            "high_risk_regression"
        ),
    }
    repository = install_repository(
        monkeypatch,
        get_candidate_for_update=AsyncMock(return_value=candidate),
        get_active_bundle_for_update=AsyncMock(return_value=base),
        get_latest_evaluated_candidate_for_update=AsyncMock(
            return_value=SimpleNamespace(
                id=command.previous_experiment_candidate_id
            )
        ),
        get_frozen_dataset_layers_for_update=AsyncMock(
            return_value=expected_layers
        ),
    )

    outcome = await ProductionBundleApplicationService().record_evaluation(
        object(), command=command, now=NOW
    )

    assert outcome.created is True
    assert outcome.candidate.status == "evaluation_passed"
    repository.insert_evaluation.assert_awaited_once()
    repository.update_candidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_approval_and_activation_are_separate_ledger_transitions(
    monkeypatch,
) -> None:
    base = active_bundle()
    domain = ProductionBundleDomainService()
    candidate = domain.propose_candidate(
        base_bundle=base,
        command=proposal(base),
        now=NOW,
    )
    evaluated = domain.record_evaluation(
        candidate=candidate,
        command=evaluation_command(candidate),
        now=NOW,
    )
    approve = ApproveCandidateCommand(
        tenant_id="tenant-1",
        candidate_id=evaluated.candidate.id,
        evaluation_run_id=evaluated.evaluation_run.id,
        approved_by="operator-1",
        reason="三层门禁已通过",
        expected_candidate_revision=evaluated.candidate.revision,
        idempotency_key="approve-request-1",
    )
    approval_repository = install_repository(
        monkeypatch,
        get_candidate_for_update=AsyncMock(
            return_value=evaluated.candidate
        ),
        get_evaluation_for_update=AsyncMock(
            return_value=evaluated.evaluation_run
        ),
    )

    reviewed = await ProductionBundleApplicationService().approve_candidate(
        object(), command=approve, now=NOW
    )

    assert reviewed.candidate.status == "approved"
    approval_repository.insert_bundle.assert_not_awaited()

    activate = ActivateCandidateCommand(
        tenant_id="tenant-1",
        candidate_id=reviewed.candidate.id,
        evaluation_run_id=evaluated.evaluation_run.id,
        activated_by="operator-1",
        reason="人工确认发布",
        expected_candidate_revision=reviewed.candidate.revision,
        idempotency_key="activate-request-1",
    )
    activation_repository = install_repository(
        monkeypatch,
        get_candidate_for_update=AsyncMock(
            return_value=reviewed.candidate
        ),
        get_evaluation_for_update=AsyncMock(
            return_value=evaluated.evaluation_run
        ),
        get_active_bundle_for_update=AsyncMock(return_value=base),
    )

    activated = await ProductionBundleApplicationService().activate_candidate(
        object(), command=activate, now=NOW
    )

    assert activated.activated_bundle.status == "active"
    assert activated.previous_bundle.status == "inactive"
    activation_repository.insert_bundle.assert_awaited_once()
    assert activation_repository.update_bundle_lifecycle.await_count == 1
