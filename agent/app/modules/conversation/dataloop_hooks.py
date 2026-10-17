"""Thin, confirmed adapters for existing candidate and Data Loop applications.

Only registered, trusted asset templates can become candidates. Analysis-input
replay cannot evaluate reranker changes, so those requests fail explicitly.
Frozen Fresh supplies a window and lineage template; the existing Workflow
creates its own Fresh dataset instead of claiming to reuse that dataset ID.
"""

from collections.abc import Awaitable, Callable, Mapping
import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator
from sqlalchemy import func, select
from temporalio.service import RPCError

from app.modules.conversation.admission_contracts import AdmissionConflictError, unavailable_admission_result
from app.modules.conversation.receipts import CapabilityReceipt
from app.modules.data_loop.bundle_contract import UnsupportedProductionBundleRuntimeError
from app.modules.data_loop.persistence.analysis_feedback import AnalysisFeedbackCaseRecord
from app.modules.data_loop.persistence.evaluation_dataset import EvaluationDatasetCaseRecord, EvaluationDatasetRecord
from app.modules.data_loop.persistence.evaluation_dataset_repository import PostgresEvaluationDatasetRepository
from app.modules.data_loop.persistence.production_bundle_repository import PostgresProductionBundleRepository
from app.modules.data_loop.contracts.production_bundle import ConfigurationDiff, ProductionBundleSpec, ProposeConfigurationCandidateCommand
from app.modules.data_loop.orchestrator import DataLoopOrchestrator, DataLoopWorkflowAccessError
from app.modules.data_loop.step_handler import DEFAULT_MAX_AUTOMATIC_FREEZE_CASES, EvaluationGatePolicyRegistry
from app.modules.data_loop.production_bundle import (
    ASSET_SPEC_FIELDS, ProductionBundleApplicationService, ProductionBundleNotFoundError,
    ProductionBundleRuleViolation, canonical_sha256,
)
from app.modules.data_loop.workflow_contracts import DataLoopRunRequest


UuidText = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
VersionText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
RERANK_PARAMETERS = {"vector_weight", "entity_weight", "event_weight", "keyword_weight", "time_weight", "minimum_score", "reranker_policy_version"}


class DataLoopEvaluationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: UuidText


class DataLoopStatusArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    workflow_id: Annotated[str, StringConstraints(pattern=r"^hot-news-data-loop-[0-9a-f]{64}$")]


class BundleCandidateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    parameter: Literal["model_version", "analysis_prompt_version", "reranker_policy_version",
                       "vector_weight", "entity_weight", "event_weight", "keyword_weight", "time_weight", "minimum_score"]
    value: VersionText | float
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]

    @model_validator(mode="after")
    def value_matches_parameter(self):
        if self.parameter in {"model_version", "analysis_prompt_version", "reranker_policy_version"}:
            if not isinstance(self.value, str):
                raise ValueError("asset version must be a string")
        elif not isinstance(self.value, float) or not 0 <= self.value <= 1:
            raise ValueError("reranker weight must be a finite number between zero and one")
        return self


class DataLoopHookUnavailable(ProductionBundleRuleViolation):
    """An explicit unpersisted outcome, with a safe receipt for tool projection."""

    def __init__(self, code: str, reason: str, *, entity_type="candidate"):
        super().__init__(code)
        self.error_code = code
        self.reason = reason
        self.result = {"reason": reason, "receipt": unpersisted_receipt(entity_type)}


def unpersisted_receipt(entity_type: str) -> dict:
    return CapabilityReceipt(entity_type=entity_type, entity_id=None, status="not_landed",
        version=None, deep_link=None, created_at=datetime.now(timezone.utc)).model_dump(mode="json")


class DataLoopCatalog:
    """Read authoritative candidate, baseline and already frozen tenant cohorts."""

    def __init__(self, *, database, runtime_registry):
        self.database = database
        self.runtime_registry = runtime_registry

    async def active_bundle(self, *, tenant_id):
        async with self.database.session() as session:
            base = await PostgresProductionBundleRepository(session).get_active_bundle(tenant_id=tenant_id)
        if base is None or base.tenant_id != tenant_id or base.status != "active":
            raise ProductionBundleNotFoundError("active production bundle is unavailable")
        return base

    async def evaluation_context(self, *, tenant_id, candidate_id):
        async with self.database.session() as session:
            bundles = PostgresProductionBundleRepository(session)
            base = await bundles.get_active_bundle(tenant_id=tenant_id)
            candidate = await bundles.get_candidate(tenant_id=tenant_id, candidate_id=UUID(candidate_id))
            if (base is None or candidate is None or base.tenant_id != tenant_id or candidate.tenant_id != tenant_id
                    or base.status != "active" or str(candidate.id) != candidate_id):
                raise ProductionBundleNotFoundError("candidate or active baseline is unavailable")
            if candidate.base_bundle_id != base.id or candidate.status not in {"pending_evaluation", "evaluation_failed", "evaluation_passed"}:
                raise ProductionBundleRuleViolation("candidate is not evaluable against the active baseline")
            self.runtime_registry.ensure_transition_supported(baseline=base.spec, candidate=candidate.proposed_spec)
            datasets = PostgresEvaluationDatasetRepository(session)
            cohorts = {}
            for layer in ("golden", "fresh_bad_case", "high_risk_regression"):
                row = await session.execute(select(EvaluationDatasetRecord.id).where(
                    EvaluationDatasetRecord.tenant_id == tenant_id,
                    EvaluationDatasetRecord.dataset_layer == layer,
                    EvaluationDatasetRecord.status == "frozen",
                ).order_by(EvaluationDatasetRecord.frozen_at.desc(), EvaluationDatasetRecord.id.desc()).limit(1))
                dataset_id = row.scalar_one_or_none()
                reference = None if dataset_id is None else await datasets.get_by_id(tenant_id=tenant_id, dataset_id=dataset_id)
                if (reference is None or reference.tenant_id != tenant_id or reference.dataset_layer != layer
                        or reference.dataset_id != dataset_id):
                    raise DataLoopHookUnavailable("evaluation_datasets_not_ready",
                        "当前租户缺少完整的已冻结黄金集、错误案例集或高风险集，请先在评审台准备数据。", entity_type="evaluation_run")
                cohorts[layer] = reference
            if len({item.dataset_id for item in cohorts.values()}) != 3:
                raise ProductionBundleRuleViolation("evaluation cohort identities must be distinct")
            fresh = cohorts["fresh_bad_case"]
            rows = await session.execute(select(func.min(AnalysisFeedbackCaseRecord.occurred_at)).join(
                EvaluationDatasetCaseRecord,
                (EvaluationDatasetCaseRecord.feedback_case_id == AnalysisFeedbackCaseRecord.id)
                & (EvaluationDatasetCaseRecord.tenant_id == AnalysisFeedbackCaseRecord.tenant_id),
            ).where(AnalysisFeedbackCaseRecord.tenant_id == tenant_id,
                EvaluationDatasetCaseRecord.tenant_id == tenant_id,
                EvaluationDatasetCaseRecord.dataset_id == fresh.dataset_id))
            start, end = rows.scalar_one_or_none(), fresh.source_cutoff_at
            if start is None or start >= end or end > datetime.now(timezone.utc):
                raise ProductionBundleRuleViolation("frozen Fresh has no valid past feedback window")
            indexes = await datasets.list_case_indexes(tenant_id=tenant_id, dataset_id=fresh.dataset_id)
            if (any(item.tenant_id != tenant_id or item.dataset_id != fresh.dataset_id
                    or item.dataset_layer != "fresh_bad_case" for item in indexes)
                    or len(indexes) != fresh.case_count):
                raise ProductionBundleRuleViolation("frozen Fresh case lineage is inconsistent")
            approved = await datasets.list_approved_case_ids_for_window(tenant_id=tenant_id,
                occurred_start=start, occurred_end=end, source_cutoff_at=end,
                limit=DEFAULT_MAX_AUTOMATIC_FREEZE_CASES + 1)
            if (not approved or len(approved) > DEFAULT_MAX_AUTOMATIC_FREEZE_CASES
                    or set(approved) != {item.feedback_case_id for item in indexes}):
                raise ProductionBundleRuleViolation("frozen Fresh no longer matches the bounded approved feedback window")
            policy = EvaluationGatePolicyRegistry().get("hot-news-gate-v1")
            return {"tenant_id": tenant_id, "candidate_id": str(candidate.id),
                "candidate_sha256": candidate.content_sha256, "base_bundle_id": str(base.id),
                "base_bundle_version": base.bundle_version, "base_bundle_sha256": base.content_sha256,
                "cohorts": {layer: {"dataset_id": str(item.dataset_id), "content_sha256": item.content_sha256,
                    "case_count": item.case_count, "dataset_version": item.dataset_version}
                    for layer, item in cohorts.items()},
                "window_start": start.isoformat(), "window_end": end.isoformat(),
                "fresh_case_ids": sorted(map(str, approved)),
                "evaluation_policy_version": policy.policy_version,
                "previous_experiment_candidate_id": None}


class ConversationDataLoopHooks:
    def __init__(self, *, database, orchestrator, runtime_registry, candidate_templates: Mapping | None = None,
                 bundle_service=None, catalog=None, admission=None):
        self.database, self.orchestrator = database, orchestrator
        self.admission = admission
        self.runtime_registry = runtime_registry
        self.catalog = catalog or DataLoopCatalog(database=database, runtime_registry=runtime_registry)
        self.bundle_service = bundle_service or ProductionBundleApplicationService()
        # The templates are trusted deployment values, never conversation input.
        self.candidate_templates = {key: ProductionBundleSpec.model_validate(value).model_copy(deep=True)
                                   for key, value in (candidate_templates or {}).items()}
        for template in self.candidate_templates.values():
            self.runtime_registry.ensure_supported(template)

    @property
    def candidate_enabled(self):
        return bool(self.candidate_templates)

    async def prepare_candidate(self, *, tenant_id, proposal):
        arguments = BundleCandidateArguments.model_validate(proposal)
        if arguments.parameter in RERANK_PARAMETERS:
            raise DataLoopHookUnavailable("reranker_candidate_unsupported",
                "当前评测只回放已冻结的分析输入，无法评测关联新闻重排参数变化；没有创建候选，也没有修改规则。")
        template = self.candidate_templates.get((arguments.parameter, arguments.value))
        if template is None:
            raise DataLoopHookUnavailable("candidate_template_unavailable", "该参数值没有已注册的可信候选模板，没有创建候选。")
        base = await self.catalog.active_bundle(tenant_id=tenant_id)
        self.runtime_registry.ensure_transition_supported(baseline=base.spec, candidate=template)
        # Derive every changed field from complete trusted snapshots, including
        # any required profile binding, and show the actual diff for confirmation.
        proposed = ProductionBundleSpec.model_validate(deepcopy(template.model_dump()))
        if getattr(proposed, arguments.parameter) != arguments.value:
            raise ProductionBundleRuleViolation("candidate template does not implement the requested parameter")
        changes = tuple(ConfigurationDiff(asset=asset, before_version=getattr(base.spec, field),
            after_version=getattr(proposed, field), reason=arguments.reason)
            for asset, field in ASSET_SPEC_FIELDS.items() if getattr(base.spec, field) != getattr(proposed, field))
        if not changes:
            raise ProductionBundleRuleViolation("candidate must change a registered asset")
        context = {"tenant_id": tenant_id, "base_bundle_id": str(base.id),
            "base_bundle_version": base.bundle_version, "base_bundle_sha256": base.content_sha256,
            "proposed_spec": proposed.model_dump(), "structured_diff": [item.model_dump() for item in changes]}
        return {"suggested": arguments.model_dump(), "prepared_context": context,
            "confirmation_fields": {"base_bundle_id": str(base.id), "base_bundle_version": base.bundle_version,
                "structured_diff": context["structured_diff"], "reason": arguments.reason,
                "notice": "仅提交待评测候选；评测、人工批准和激活仍在评审台完成。"},
            "receipt": unpersisted_receipt("candidate")}

    async def start_candidate(self, *, tenant_id, user_id, conversation_id, request_id, proposal,
                              prepared_context, on_bound: Callable[[dict], Awaitable[None]] | None = None):
        prepared = await self.prepare_candidate(tenant_id=tenant_id, proposal=proposal)
        if prepared["prepared_context"] != prepared_context:
            raise ProductionBundleRuleViolation("candidate_base_changed")
        conversation, request = UUID(str(conversation_id)), UUID(str(request_id))
        request_digest = canonical_sha256({"conversation_id": str(conversation), "request_id": str(request)})
        arguments = BundleCandidateArguments.model_validate(proposal)
        async with self.database.session() as session:
            # The same active row is locked as the existing promotion services.
            base = await PostgresProductionBundleRepository(session).get_active_bundle_for_update(tenant_id=tenant_id)
            if base is None or str(base.id) != prepared_context["base_bundle_id"] or base.content_sha256 != prepared_context["base_bundle_sha256"]:
                raise ProductionBundleRuleViolation("candidate_base_changed")
            outcome = await self.bundle_service.propose_candidate(session,
                command=ProposeConfigurationCandidateCommand(tenant_id=tenant_id, base_bundle_id=base.id,
                    candidate_version=f"conversation-{request_digest}",
                    proposed_spec=ProductionBundleSpec.model_validate(prepared_context["proposed_spec"]),
                    structured_diff=tuple(ConfigurationDiff.model_validate(item) for item in prepared_context["structured_diff"]),
                    proposed_by=str(UUID(user_id)), proposal_reason=arguments.reason,
                    idempotency_key=f"conversation-candidate:{conversation}:{request}"), now=datetime.now(timezone.utc))
        candidate = outcome.candidate
        result = {"candidate_id": str(candidate.id), "candidate_version": candidate.candidate_version,
            "base_bundle_id": str(candidate.base_bundle_id), "status": candidate.status,
            "structured_diff": [item.model_dump(mode="json") for item in candidate.structured_diff],
            "created": outcome.created, "snapshot_at": candidate.created_at.isoformat(),
            "receipt": CapabilityReceipt(entity_type="candidate", entity_id=str(candidate.id),
                status="pending_approval", version=candidate.content_sha256,
                deep_link=f"/data-loop?candidate_id={candidate.id}", pending_gate="candidate_evaluation",
                created_at=candidate.created_at).model_dump(mode="json")}
        if on_bound is not None:
            await on_bound(result)
        return result

    async def prepare_evaluation(self, *, tenant_id, proposal):
        arguments = DataLoopEvaluationArguments.model_validate(proposal)
        context = await self.catalog.evaluation_context(tenant_id=tenant_id, candidate_id=arguments.candidate_id)
        return {"suggested": arguments.model_dump(), "prepared_context": context,
            "confirmation_fields": {"candidate_id": context["candidate_id"],
                "base_bundle_version": context["base_bundle_version"], "cohorts": context["cohorts"],
                "window_start": context["window_start"], "window_end": context["window_end"],
                "evaluation_policy_version": context["evaluation_policy_version"],
                "notice": "复用已冻结黄金集和高风险集；按展示的来源窗口新冻结错误案例集，评测通过后仍需人工批准。"},
            "receipt": unpersisted_receipt("evaluation_run")}

    async def start_evaluation(self, *, tenant_id, user_id, conversation_id, request_id, proposal,
                               prepared_context, on_bound: Callable[[dict], Awaitable[None]] | None = None):
        prepared = await self.prepare_evaluation(tenant_id=tenant_id, proposal=proposal)
        if prepared["prepared_context"] != prepared_context:
            raise ProductionBundleRuleViolation("evaluation_context_changed")
        context = prepared["prepared_context"]
        conversation, request = UUID(str(conversation_id)), UUID(str(request_id))
        request_digest = canonical_sha256({"conversation_id": str(conversation), "request_id": str(request)})
        UUID(user_id)  # The actor is trusted gateway context, never a tool argument.
        command = DataLoopRunRequest(tenant_id=tenant_id,
            window_start=datetime.fromisoformat(context["window_start"]),
            window_end=datetime.fromisoformat(context["window_end"]),
            dataset_name="conversation-fresh-bad-case", dataset_version=f"confirmed-{request_digest[:48]}",
            golden_dataset_id=context["cohorts"]["golden"]["dataset_id"],
            high_risk_regression_dataset_id=context["cohorts"]["high_risk_regression"]["dataset_id"],
            candidate_id=context["candidate_id"], previous_experiment_candidate_id=context["previous_experiment_candidate_id"],
            evaluation_policy_version=context["evaluation_policy_version"],
            idempotency_key=f"conversation-data-loop:{conversation}:{request}")
        command.validate()
        expected_id = DataLoopOrchestrator.workflow_id(command)
        partial = {"workflow_id": expected_id, "workflow_accepted": False,
            "run": {"workflow_id": expected_id, "phase": "starting", "gate_passed": None,
                    "waiting_for_approval": False, "evaluation_run_id": None},
            "receipt": unpersisted_receipt("evaluation_run")}
        if on_bound is not None:
            await on_bound(partial)
        admission = None
        if self.admission is None:
            workflow_id = await self.orchestrator.start(command)
        else:
            admission = await self.admission.start_evaluation(command)
            workflow_id = admission["workflow_id"]
        if workflow_id != expected_id:
            raise ProductionBundleRuleViolation("evaluation_workflow_identity_mismatch")
        accepted = admission["workflow_accepted"] if admission is not None else True
        result = {"workflow_id": workflow_id, "workflow_accepted": accepted,
            "candidate_id": context["candidate_id"], "evaluation_policy_version": command.evaluation_policy_version,
            "run": {"workflow_id": workflow_id, "phase": "accepted" if accepted else admission["admission"]["state"], "gate_passed": None,
                    "waiting_for_approval": False, "evaluation_run_id": None},
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "receipt": (self._run_receipt(workflow_id, version=command.evaluation_policy_version)
                        if accepted else unpersisted_receipt("evaluation_run"))}
        if admission is not None:
            result.update({key: admission[key] for key in ("admission", "admission_accepted")})
        if on_bound is not None:
            await on_bound(result)
        if admission is not None and not admission["admission_accepted"]:
            raise ProductionBundleRuleViolation(admission["admission"].get("error_code") or "admission_rejected")
        return result

    async def status(self, *, tenant_id, proposal):
        arguments = DataLoopStatusArguments.model_validate(proposal)
        admission = None
        request_verified = False
        verification_error = None
        if self.admission is not None:
            try:
                async with asyncio.timeout(2):
                    observed = await self.admission.status(tenant_id=tenant_id,
                        business_workflow_id=arguments.workflow_id)
                admission = None if observed.get("not_found") else observed
            except AdmissionConflictError as exc:
                raise DataLoopWorkflowAccessError("Data Loop workflow was not found") from exc
            except Exception:
                admission = unavailable_admission_result(tenant_id, arguments.workflow_id)
                try:
                    async with asyncio.timeout(2):
                        verified = await self.admission.verify_request(tenant_id=tenant_id,
                            business_workflow_id=arguments.workflow_id)
                    request_verified = verified.get("not_found") is False
                except AdmissionConflictError as exc:
                    raise DataLoopWorkflowAccessError("Data Loop workflow was not found") from exc
                except Exception as exc:
                    verification_error = exc
            if admission is not None and admission["workflow_accepted"] is False:
                result = {"workflow_id": arguments.workflow_id,
                    **{key: admission[key] for key in ("admission", "admission_accepted", "workflow_accepted")},
                    "run": {"workflow_id": arguments.workflow_id, "phase": admission["admission"]["state"],
                        "gate_passed": None, "waiting_for_approval": False, "evaluation_run_id": None},
                    "snapshot_at": datetime.now(timezone.utc).isoformat(),
                    "receipt": unpersisted_receipt("evaluation_run")}
                if admission.get("evaluation_policy_version") is not None:
                    result["evaluation_policy_version"] = admission["evaluation_policy_version"]
                return result
        try:
            if admission is not None and admission["workflow_accepted"] is None:
                async with asyncio.timeout(2):
                    snapshot = await self.orchestrator.get_snapshot(arguments.workflow_id, tenant_id=tenant_id)
            else:
                snapshot = await self.orchestrator.get_snapshot(arguments.workflow_id, tenant_id=tenant_id)
        except (RPCError, TimeoutError):
            if request_verified:
                # A persisted, tenant-verified request can still be queued while
                # its actor worker/query is unavailable. It proves no business
                # execution, so retain a gray receipt and unknown acceptance.
                return {"workflow_id": arguments.workflow_id, **admission,
                    "run": {"workflow_id": arguments.workflow_id, "phase": "unknown",
                        "gate_passed": None, "waiting_for_approval": False, "evaluation_run_id": None},
                    "snapshot_at": datetime.now(timezone.utc).isoformat(),
                    "receipt": unpersisted_receipt("evaluation_run")}
            if isinstance(verification_error, (RPCError, TimeoutError)):
                raise verification_error
            raise
        if snapshot.tenant_id != tenant_id:
            raise DataLoopWorkflowAccessError("Data Loop workflow was not found")
        run = {"workflow_id": arguments.workflow_id, "phase": snapshot.phase,
            "gate_passed": snapshot.gate_passed, "waiting_for_approval": snapshot.waiting_for_approval,
            "dataset_id": snapshot.dataset_id, "evaluation_run_id": snapshot.evaluation_run_id,
            "dataset_analysis_status": snapshot.dataset_analysis_status}
        result = {"workflow_id": arguments.workflow_id, "workflow_accepted": True, "run": run,
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "receipt": self._run_receipt(arguments.workflow_id, version=canonical_sha256(asdict(snapshot)),
                                         waiting=snapshot.waiting_for_approval)}
        if admission is not None:
            result.update({key: admission[key] for key in ("admission", "admission_accepted", "workflow_accepted")})
            # A successful tenant-checked query proves the business Workflow
            # exists even when admission history is temporarily unreadable.
            result["workflow_accepted"] = True
        return result

    @staticmethod
    def _run_receipt(workflow_id, *, version, waiting=False):
        return CapabilityReceipt(entity_type="evaluation_run", entity_id=workflow_id,
            status="pending_approval" if waiting else "landed", version=version,
            deep_link=f"/data-loop?workflow_id={workflow_id}",
            pending_gate="bundle_promotion" if waiting else None,
            created_at=datetime.now(timezone.utc)).model_dump(mode="json")
