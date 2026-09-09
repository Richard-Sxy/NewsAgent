from datetime import datetime, timedelta, timezone
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

import app.services.production_bundle_application as application_module
from app.repositories.production_bundle import PostgresProductionBundleRepository
from app.schemas.production_bundle import (
    ConfigurationCandidate,
    ConfigurationDiff,
    ProductionBundle,
    ProductionBundleSpec,
    ProposeConfigurationCandidateCommand,
)
from app.services.production_bundle import (
    ProductionBundleDomainService,
    bundle_spec_sha256,
    command_fingerprint,
)
from app.services.production_bundle_application import (
    ProductionBundleApplicationService,
    ProductionBundleConflictError,
    ProductionBundleTargetNotFoundError,
)


NOW = datetime(2026, 9, 9, 8, tzinfo=timezone.utc)


def spec() -> ProductionBundleSpec:
    return ProductionBundleSpec(
        metric_definition_version="metric-v1",
        hot_score_policy_version="score-v1",
        reranker_policy_version="rerank-v1",
        analysis_prompt_version="prompt-v1",
        fastgpt_app_id="app-v1",
        model_version="model-v1",
        output_schema_version="schema-v1",
        validator_version="validator-v1",
        memory_resolver_policy_version="memory-v1",
    )


def bundle() -> ProductionBundle:
    value = spec()
    return ProductionBundle(
        id=uuid4(),
        tenant_id="tenant-1",
        bundle_version="bundle-v1",
        spec=value,
        content_sha256=bundle_spec_sha256(value),
        status="active",
        created_by="operator-0",
        created_at=NOW - timedelta(days=1),
        activated_by="operator-0",
        activated_at=NOW - timedelta(days=1),
    )


def proposal(base: ProductionBundle) -> ProposeConfigurationCandidateCommand:
    return ProposeConfigurationCandidateCommand(
        tenant_id=base.tenant_id,
        base_bundle_id=base.id,
        candidate_version="bundle-v2",
        proposed_spec=base.spec.model_copy(
            update={"analysis_prompt_version": "prompt-v2"}
        ),
        structured_diff=(
            ConfigurationDiff(
                asset="analysis_prompt",
                before_version="prompt-v1",
                after_version="prompt-v2",
                reason="修复 bad case",
            ),
        ),
        proposed_by="attribution-agent",
        proposal_reason="提示词边界不清晰",
        idempotency_key="candidate-request-1",
    )


def repository_mock(monkeypatch):
    repository = create_autospec(
        PostgresProductionBundleRepository,
        instance=True,
    )
    monkeypatch.setattr(
        application_module,
        "PostgresProductionBundleRepository",
        lambda session: repository,
    )
    return repository


@pytest.mark.asyncio
async def test_candidate_proposal_locks_base_and_is_idempotent(monkeypatch):
    base = bundle()
    command = proposal(base)
    expected = ProductionBundleDomainService().propose_candidate(
        base_bundle=base,
        command=command,
        now=NOW,
    )
    domain = create_autospec(ProductionBundleDomainService, instance=True)
    domain.propose_candidate.return_value = expected
    repository = repository_mock(monkeypatch)
    repository.get_candidate_by_idempotency_key.side_effect = [None, None]
    repository.get_bundle_for_update.return_value = base
    repository.insert_candidate.return_value = True

    outcome = await ProductionBundleApplicationService(domain).propose_candidate(
        object(),
        command=command,
        now=NOW,
    )

    assert outcome.created is True
    assert outcome.candidate is expected
    repository.get_bundle_for_update.assert_awaited_once_with(
        tenant_id=base.tenant_id,
        bundle_id=base.id,
    )
    repository.insert_candidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_candidate_request_replays_without_writes(monkeypatch):
    base = bundle()
    command = proposal(base)
    candidate = ProductionBundleDomainService().propose_candidate(
        base_bundle=base,
        command=command,
        now=NOW,
    )
    repository = repository_mock(monkeypatch)
    repository.get_candidate_by_idempotency_key.return_value = (
        candidate,
        command_fingerprint(command),
    )

    outcome = await ProductionBundleApplicationService().propose_candidate(
        object(),
        command=command,
        now=NOW,
    )

    assert outcome.created is False
    assert outcome.candidate == candidate
    repository.get_bundle_for_update.assert_not_awaited()
    repository.insert_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_reused_idempotency_key_with_other_content_conflicts(monkeypatch):
    base = bundle()
    command = proposal(base)
    candidate = ConfigurationCandidate(
        **ProductionBundleDomainService()
        .propose_candidate(
            base_bundle=base,
            command=command,
            now=NOW,
        )
        .model_dump()
    )
    repository = repository_mock(monkeypatch)
    repository.get_candidate_by_idempotency_key.return_value = (
        candidate,
        "0" * 64,
    )

    with pytest.raises(ProductionBundleConflictError, match="idempotency"):
        await ProductionBundleApplicationService().propose_candidate(
            object(),
            command=command,
            now=NOW,
        )

    repository.insert_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_tenant_or_missing_base_is_not_disclosed(monkeypatch):
    base = bundle()
    command = proposal(base)
    repository = repository_mock(monkeypatch)
    repository.get_candidate_by_idempotency_key.return_value = None
    repository.get_bundle_for_update.return_value = None

    with pytest.raises(ProductionBundleTargetNotFoundError, match="not found"):
        await ProductionBundleApplicationService().propose_candidate(
            object(),
            command=command,
            now=NOW,
        )

    repository.get_bundle_for_update.assert_awaited_once_with(
        tenant_id="tenant-1",
        bundle_id=base.id,
    )
