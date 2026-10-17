"""Data Loop hooks preserve runtime, frozen cohort and manual release boundaries."""

import asyncio

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import app.modules.conversation.dataloop_hooks as hook_module
import app.modules.data_loop.production_bundle as bundle_module
from app.modules.conversation.dataloop_hooks import (
    BundleCandidateArguments, ConversationDataLoopHooks, DataLoopCatalog, DataLoopHookUnavailable,
)
from app.modules.data_loop.bundle_contract import UnsupportedProductionBundleRuntimeError
from app.modules.data_loop.bundle_runtime import NativeProductionBundleRuntimeRegistry
from app.model_runtime.core import PromptRegistry, PromptSpec
from app.modules.hot_news.analysis.runner import HOT_NEWS_SCENE
from app.modules.data_loop.persistence.evaluation_dataset_repository import PostgresEvaluationDatasetRepository
from app.modules.data_loop.persistence.production_bundle_repository import PostgresProductionBundleRepository
from app.modules.data_loop.contracts.evaluation_dataset import EvaluationDatasetCaseIndex, EvaluationDatasetReference
from app.modules.data_loop.orchestrator import DataLoopOrchestrator, DataLoopWorkflowAccessError
from app.modules.data_loop.production_bundle import ProductionBundleApplicationService, ProductionBundleRuleViolation
from app.modules.data_loop.workflow_contracts import DataLoopWorkflowSnapshot
from tests.test_production_bundle import active_bundle, spec


TENANT = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
NOW = datetime.now(timezone.utc)


class Database:
    def __init__(self):
        self.commits = 0

    @asynccontextmanager
    async def session(self):
        yield self
        self.commits += 1


class Catalog:
    def __init__(self, base, context):
        self.base, self.context = base, context
        self.calls = []

    async def active_bundle(self, *, tenant_id):
        assert tenant_id == self.base.tenant_id
        self.calls.append(("base", tenant_id))
        return self.base

    async def evaluation_context(self, *, tenant_id, candidate_id):
        assert tenant_id == self.context["tenant_id"] and candidate_id == self.context["candidate_id"]
        self.calls.append(("evaluation", tenant_id, candidate_id))
        return deepcopy(self.context)


def runtime_registry(base, candidate):
    prompts = PromptRegistry((PromptSpec(HOT_NEWS_SCENE, "prompt-v1", "Controlled test prompt"),))
    return NativeProductionBundleRuntimeRegistry(None, prompts, (base.spec, candidate),
        allowed_model_routes=("model-v1", "model-v2"))


def harness(monkeypatch, *, templates=True):
    base = active_bundle(tenant_id=TENANT)
    proposed = spec(model_version="model-v2")
    context = {"tenant_id": TENANT, "candidate_id": str(uuid4()), "candidate_sha256": "a" * 64,
        "base_bundle_id": str(base.id), "base_bundle_version": base.bundle_version,
        "base_bundle_sha256": base.content_sha256,
        "cohorts": {layer: {"dataset_id": str(uuid4()), "content_sha256": digest * 64,
            "case_count": 1, "dataset_version": "frozen-v1"}
            for layer, digest in (("golden", "b"), ("fresh_bad_case", "c"), ("high_risk_regression", "d"))},
        "window_start": (NOW - timedelta(hours=2)).isoformat(), "window_end": (NOW - timedelta(hours=1)).isoformat(),
        "fresh_case_ids": [str(uuid4())], "evaluation_policy_version": "hot-news-gate-v1",
        "previous_experiment_candidate_id": None}
    catalog = Catalog(base, context)
    registry = runtime_registry(base, proposed)
    database = Database()
    repository = create_autospec(PostgresProductionBundleRepository, instance=True)
    candidates = {}
    repository.get_active_bundle_for_update.return_value = base
    repository.get_bundle_for_update.return_value = base
    async def replay(*, tenant_id, idempotency_key):
        return candidates.get((tenant_id, idempotency_key))
    async def insert(*, candidate, idempotency_key, request_fingerprint):
        candidates[(candidate.tenant_id, idempotency_key)] = candidate, request_fingerprint
        return True
    repository.get_candidate_by_idempotency_key.side_effect = replay
    repository.insert_candidate.side_effect = insert
    monkeypatch.setattr(hook_module, "PostgresProductionBundleRepository", lambda session: repository)
    monkeypatch.setattr(bundle_module, "PostgresProductionBundleRepository", lambda session: repository)
    handles = {}
    snapshot = DataLoopWorkflowSnapshot(tenant_id=TENANT, phase="waiting_approval", dataset_id=None,
        evaluation_run_id=str(uuid4()), gate_passed=True, waiting_for_approval=True)
    async def start_workflow(workflow, command, **kwargs):
        key = kwargs["id"]
        if key not in handles:
            description = SimpleNamespace(memo_value=AsyncMock(return_value=kwargs["memo"]["data_loop_request_fingerprint"]))
            handles[key] = SimpleNamespace(describe=AsyncMock(return_value=description), query=AsyncMock(return_value=snapshot),
                cancel=AsyncMock(), signal=AsyncMock())
        return handles[key]
    client = SimpleNamespace(start_workflow=AsyncMock(side_effect=start_workflow),
                             get_workflow_handle=lambda key: handles[key])
    orchestrator = DataLoopOrchestrator(client, SimpleNamespace(temporal_data_loop_task_queue="test-data-loop"))
    hooks = ConversationDataLoopHooks(database=database, orchestrator=orchestrator, runtime_registry=registry,
        candidate_templates={("model_version", "model-v2"): proposed} if templates else {},
        bundle_service=ProductionBundleApplicationService(), catalog=catalog)
    identity = dict(tenant_id=TENANT, user_id=USER, conversation_id=uuid4(), request_id=uuid4())
    return hooks, identity, base, proposed, context, catalog, database, repository, candidates, client, handles


@pytest.mark.asyncio
async def test_candidate_prepare_uses_registered_template_and_python_actual_diff_without_writes(monkeypatch):
    hooks, _, base, proposed, _, _, database, repository, _, _, _ = harness(monkeypatch)
    args = {"parameter": "model_version", "value": "model-v2", "reason": "比较已注册模型路由"}
    result = await hooks.prepare_candidate(tenant_id=TENANT, proposal=args)
    assert hooks.candidate_enabled and result["suggested"] == args
    assert result["prepared_context"]["base_bundle_id"] == str(base.id)
    assert result["prepared_context"]["proposed_spec"] == proposed.model_dump()
    assert result["confirmation_fields"]["structured_diff"] == [{"asset": "model", "before_version": "model-v1",
        "after_version": "model-v2", "reason": args["reason"]}]
    assert result["receipt"]["status"] == "not_landed" and database.commits == 0
    repository.insert_candidate.assert_not_awaited()
    assert base.spec.model_version == "model-v1"


@pytest.mark.asyncio
async def test_candidate_confirm_persists_pending_evaluation_after_commit_and_replays(monkeypatch):
    hooks, identity, _, _, _, _, database, repository, candidates, _, _ = harness(monkeypatch)
    args = {"parameter": "model_version", "value": "model-v2", "reason": "比较已注册模型路由"}
    prepared = await hooks.prepare_candidate(tenant_id=TENANT, proposal=args)
    bindings = []
    async def bound(result):
        assert database.commits and len(candidates) == 1
        bindings.append(result)
    first = await hooks.start_candidate(**identity, proposal=args, prepared_context=prepared["prepared_context"], on_bound=bound)
    replay = await hooks.start_candidate(**identity, proposal=args, prepared_context=prepared["prepared_context"])
    assert first["candidate_id"] == replay["candidate_id"] and len(candidates) == 1
    assert first["status"] == "pending_evaluation" and first["receipt"]["pending_gate"] == "candidate_evaluation"
    assert first["receipt"]["deep_link"] == f"/data-loop?candidate_id={first['candidate_id']}"
    assert bindings == [first]
    repository.update_bundle_lifecycle.assert_not_awaited()
    repository.insert_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_active_bundle_invalidates_confirmation_before_candidate_write(monkeypatch):
    hooks, identity, _, _, _, catalog, _, repository, _, _, _ = harness(monkeypatch)
    args = {"parameter": "model_version", "value": "model-v2", "reason": "比较已注册模型路由"}
    prepared = await hooks.prepare_candidate(tenant_id=TENANT, proposal=args)
    catalog.base = active_bundle(tenant_id=TENANT)
    with pytest.raises(ProductionBundleRuleViolation, match="candidate_base_changed"):
        await hooks.start_candidate(**identity, proposal=args, prepared_context=prepared["prepared_context"])
    repository.insert_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_candidate_checks_locked_baseline_again_before_proposal_service(monkeypatch):
    hooks, identity, _, _, _, _, _, repository, _, _, _ = harness(monkeypatch)
    args = {"parameter": "model_version", "value": "model-v2", "reason": "比较已注册模型路由"}
    prepared = await hooks.prepare_candidate(tenant_id=TENANT, proposal=args)
    repository.get_active_bundle_for_update.return_value = active_bundle(tenant_id=TENANT)
    with pytest.raises(ProductionBundleRuleViolation, match="candidate_base_changed"):
        await hooks.start_candidate(**identity, proposal=args, prepared_context=prepared["prepared_context"])
    repository.insert_candidate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter,value", [("vector_weight", 0.6), ("minimum_score", 0.5), ("reranker_policy_version", "rerank-v2")])
async def test_reranker_request_cannot_be_replaced_with_model_candidate(monkeypatch, parameter, value):
    hooks, _, _, _, _, catalog, _, repository, _, _, _ = harness(monkeypatch)
    with pytest.raises(DataLoopHookUnavailable) as denied:
        await hooks.prepare_candidate(tenant_id=TENANT, proposal={"parameter": parameter, "value": value, "reason": "试验关联新闻参数"})
    assert denied.value.error_code == "reranker_candidate_unsupported"
    assert denied.value.result["receipt"]["status"] == "not_landed" and "无法评测" in denied.value.reason
    assert not catalog.calls
    repository.insert_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_trusted_templates_hide_candidate_capability_and_reject_arbitrary_value(monkeypatch):
    hooks, _, _, _, _, _, _, repository, _, _, _ = harness(monkeypatch, templates=False)
    assert not hooks.candidate_enabled
    with pytest.raises(DataLoopHookUnavailable, match="candidate_template_unavailable"):
        await hooks.prepare_candidate(tenant_id=TENANT, proposal={"parameter": "model_version", "value": "unapproved-model", "reason": "测试"})
    repository.insert_candidate.assert_not_awaited()


@pytest.mark.parametrize("extra", [{"value": True}, {"value": float("nan")}, {"value": 1.1},
    {"parameter": "hot_score_policy"}, {"tenant_id": TENANT}, {"proposed_spec": {}}, {"base_bundle_id": str(uuid4())}])
def test_candidate_model_contract_refuses_identity_full_spec_and_out_of_range_values(extra):
    with pytest.raises(ValidationError):
        BundleCandidateArguments.model_validate({"parameter": "vector_weight", "value": 0.5, "reason": "测试", **extra})


@pytest.mark.asyncio
async def test_evaluation_prepare_freezes_actual_source_context_and_starts_no_workflow(monkeypatch):
    hooks, _, _, _, context, _, _, _, _, client, _ = harness(monkeypatch)
    prepared = await hooks.prepare_evaluation(tenant_id=TENANT, proposal={"candidate_id": context["candidate_id"]})
    assert prepared["prepared_context"] == context
    assert prepared["confirmation_fields"]["cohorts"] == context["cohorts"]
    assert "新冻结" in prepared["confirmation_fields"]["notice"]
    assert prepared["receipt"]["status"] == "not_landed"
    client.start_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluation_start_waits_only_acceptance_preserves_workflow_identity_and_uuid_idempotency(monkeypatch):
    hooks, identity, _, _, context, _, _, _, _, client, handles = harness(monkeypatch)
    proposal = {"candidate_id": context["candidate_id"]}
    prepared = await hooks.prepare_evaluation(tenant_id=TENANT, proposal=proposal)
    bindings = []
    async def bound(value):
        bindings.append(value)
    first = await hooks.start_evaluation(**identity, proposal=proposal, prepared_context=prepared["prepared_context"], on_bound=bound)
    replay = await hooks.start_evaluation(**identity, proposal=proposal, prepared_context=prepared["prepared_context"])
    assert first["workflow_id"] == replay["workflow_id"] and len(handles) == 1
    assert bindings[0]["workflow_accepted"] is False and bindings[0]["receipt"]["status"] == "not_landed"
    assert bindings[-1] == first and first["workflow_accepted"] is True
    assert first["run"]["evaluation_run_id"] is None and first["run"]["phase"] == "accepted"
    assert first["receipt"]["entity_id"] == first["workflow_id"]
    assert first["receipt"]["deep_link"] == f"/data-loop?workflow_id={first['workflow_id']}"
    command = client.start_workflow.await_args.args[1]
    assert command.golden_dataset_id == context["cohorts"]["golden"]["dataset_id"]
    assert command.high_risk_regression_dataset_id == context["cohorts"]["high_risk_regression"]["dataset_id"]
    assert command.dataset_version != context["cohorts"]["fresh_bad_case"]["dataset_version"]
    assert len(command.dataset_version) <= 64 and command.evaluation_policy_version == "hot-news-gate-v1"
    assert str(identity["conversation_id"]) in command.idempotency_key and str(identity["request_id"]) in command.idempotency_key
    handles[first["workflow_id"]].query.assert_not_awaited()
    handles[first["workflow_id"]].signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluation_context_changes_after_confirmation_reject_before_submission(monkeypatch):
    hooks, identity, _, _, context, catalog, _, _, _, client, _ = harness(monkeypatch)
    proposal = {"candidate_id": context["candidate_id"]}
    prepared = await hooks.prepare_evaluation(tenant_id=TENANT, proposal=proposal)
    catalog.context["cohorts"]["golden"]["content_sha256"] = "f" * 64
    with pytest.raises(ProductionBundleRuleViolation, match="evaluation_context_changed"):
        await hooks.start_evaluation(**identity, proposal=proposal, prepared_context=prepared["prepared_context"])
    client.start_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluation_transport_failure_retains_unacknowledged_reference_without_retry(monkeypatch):
    hooks, identity, _, _, context, _, _, _, _, client, _ = harness(monkeypatch)
    proposal = {"candidate_id": context["candidate_id"]}
    prepared = await hooks.prepare_evaluation(tenant_id=TENANT, proposal=proposal)
    client.start_workflow.side_effect = ConnectionError("Temporary Temporal failure")
    bindings = []
    async def bound(value):
        bindings.append(value)
    with pytest.raises(ConnectionError):
        await hooks.start_evaluation(**identity, proposal=proposal, prepared_context=prepared["prepared_context"], on_bound=bound)
    assert client.start_workflow.await_count == 1
    assert len(bindings) == 1 and bindings[0]["workflow_accepted"] is False
    assert bindings[0]["receipt"]["status"] == "not_landed"
    assert bindings[0]["workflow_id"].startswith("hot-news-data-loop-")


@pytest.mark.asyncio
async def test_cancelling_acceptance_wait_preserves_workflow_without_cancel_signal(monkeypatch):
    hooks, identity, _, _, context, _, _, _, _, client, handles = harness(monkeypatch)
    proposal = {"candidate_id": context["candidate_id"]}
    prepared = await hooks.prepare_evaluation(tenant_id=TENANT, proposal=proposal)
    accepted = asyncio.Event()
    underlying = client.start_workflow.side_effect
    async def slow_ack(*args, **kwargs):
        handle = await underlying(*args, **kwargs)
        accepted.set()
        await asyncio.Event().wait()
        return handle
    client.start_workflow.side_effect = slow_ack
    bindings = []
    async def bound(value):
        bindings.append(value)
    execution = asyncio.create_task(hooks.start_evaluation(**identity, proposal=proposal,
        prepared_context=prepared["prepared_context"], on_bound=bound))
    await asyncio.wait_for(accepted.wait(), timeout=2)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert len(handles) == 1 and bindings[0]["workflow_id"] in handles
    assert bindings[0]["workflow_accepted"] is False
    handle = handles[bindings[0]["workflow_id"]]
    handle.cancel.assert_not_awaited()
    handle.signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_request_uuid_in_another_conversation_gets_distinct_fresh_version(monkeypatch):
    hooks, identity, _, _, context, _, _, _, _, client, _ = harness(monkeypatch)
    proposal = {"candidate_id": context["candidate_id"]}
    prepared = await hooks.prepare_evaluation(tenant_id=TENANT, proposal=proposal)
    await hooks.start_evaluation(**identity, proposal=proposal, prepared_context=prepared["prepared_context"])
    first = client.start_workflow.await_args.args[1]
    await hooks.start_evaluation(**{**identity, "conversation_id": uuid4()}, proposal=proposal, prepared_context=prepared["prepared_context"])
    second = client.start_workflow.await_args.args[1]
    assert first.dataset_version != second.dataset_version


@pytest.mark.asyncio
async def test_evaluation_status_returns_phase_gate_and_read_only_workflow_receipt(monkeypatch):
    hooks, identity, _, _, context, _, _, _, _, _, handles = harness(monkeypatch)
    proposal = {"candidate_id": context["candidate_id"]}
    prepared = await hooks.prepare_evaluation(tenant_id=TENANT, proposal=proposal)
    started = await hooks.start_evaluation(**identity, proposal=proposal, prepared_context=prepared["prepared_context"])
    status = await hooks.status(tenant_id=TENANT, proposal={"workflow_id": started["workflow_id"]})
    assert status["run"]["phase"] == "waiting_approval" and status["run"]["gate_passed"] is True
    assert status["receipt"]["status"] == "pending_approval" and status["receipt"]["pending_gate"] == "bundle_promotion"
    assert status["receipt"]["entity_id"] == started["workflow_id"]
    handles[started["workflow_id"]].signal.assert_not_awaited()
    with pytest.raises(DataLoopWorkflowAccessError):
        await hooks.status(tenant_id=str(uuid4()), proposal={"workflow_id": started["workflow_id"]})
    with pytest.raises(ValidationError):
        await hooks.status(tenant_id=TENANT, proposal={"workflow_id": started["workflow_id"], "action": "approve"})


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"window_start": "2026-10-13"}, {"sql": "select 1"}, {"tenant_id": TENANT},
    {"golden_dataset_id": str(uuid4())}, {"evaluation_policy_version": "ungated"}])
async def test_evaluation_model_contract_cannot_supply_dates_sql_cohorts_or_policy(monkeypatch, extra):
    hooks, _, _, _, context, catalog, _, _, _, client, _ = harness(monkeypatch)
    with pytest.raises(ValidationError):
        await hooks.prepare_evaluation(tenant_id=TENANT, proposal={"candidate_id": context["candidate_id"], **extra})
    assert not catalog.calls
    client.start_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_catalog_checks_tenant_layers_case_lineage_and_registered_policy(monkeypatch):
    hooks, _, base, proposed, context, _, _, bundles, _, _, _ = harness(monkeypatch)
    candidate = SimpleNamespace(id=UUID(context["candidate_id"]), tenant_id=TENANT, base_bundle_id=base.id,
        status="pending_evaluation", proposed_spec=proposed, content_sha256="a" * 64)
    bundles.get_active_bundle.return_value = base
    bundles.get_candidate.return_value = candidate
    references = {layer: EvaluationDatasetReference(dataset_id=UUID(item["dataset_id"]), tenant_id=TENANT,
        dataset_name=f"frozen-{layer}", dataset_version="frozen-v1", dataset_layer=layer, status="frozen",
        schema_version="1.0", description="reviewed samples", source_cutoff_at=NOW-timedelta(hours=1),
        frozen_at=NOW-timedelta(minutes=30), frozen_by=USER, idempotency_key=f"frozen-{layer}",
        request_fingerprint="e"*64, content_sha256=item["content_sha256"], artifact_uri="s3://test/frozen.json",
        artifact_size=100, case_count=1) for layer, item in context["cohorts"].items()}
    fresh_id = references["fresh_bad_case"].dataset_id
    case_id = UUID(context["fresh_case_ids"][0])
    datasets = create_autospec(PostgresEvaluationDatasetRepository, instance=True)
    async def get_by_id(*, tenant_id, dataset_id):
        assert tenant_id == TENANT
        return next(item for item in references.values() if item.dataset_id == dataset_id)
    datasets.get_by_id.side_effect = get_by_id
    datasets.list_case_indexes.return_value = (EvaluationDatasetCaseIndex(dataset_id=fresh_id, tenant_id=TENANT,
        feedback_case_id=case_id, position=0, case_id=f"feedback:{case_id}", news_id="news-1", dataset_layer="fresh_bad_case",
        severity="medium", label_version=1, case_content_sha256="f"*64),)
    datasets.list_approved_case_ids_for_window.return_value = (case_id,)
    monkeypatch.setattr(hook_module, "PostgresEvaluationDatasetRepository", lambda session: datasets)
    class CatalogDatabase(Database):
        async def execute(self, statement):
            params = statement.compile().params
            assert TENANT in params.values()
            layer = next((value for value in params.values() if value in references), None)
            value = references[layer].dataset_id if layer else NOW-timedelta(hours=2)
            return SimpleNamespace(scalar_one_or_none=lambda: value)
    catalog = DataLoopCatalog(database=CatalogDatabase(), runtime_registry=hooks.runtime_registry)
    result = await catalog.evaluation_context(tenant_id=TENANT, candidate_id=context["candidate_id"])
    assert result == context
    datasets.list_approved_case_ids_for_window.assert_awaited_once_with(tenant_id=TENANT,
        occurred_start=NOW-timedelta(hours=2), occurred_end=NOW-timedelta(hours=1),
        source_cutoff_at=NOW-timedelta(hours=1), limit=21)
    bundles.get_candidate.assert_awaited_once_with(tenant_id=TENANT, candidate_id=UUID(context["candidate_id"]))
    datasets.list_approved_case_ids_for_window.return_value = (uuid4(),)
    with pytest.raises(ProductionBundleRuleViolation, match="bounded approved"):
        await catalog.evaluation_context(tenant_id=TENANT, candidate_id=context["candidate_id"])

