"""Tenant-scoped conversation adapters for the existing writing services."""

from collections.abc import Awaitable, Callable
import asyncio
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.conversation.admission_contracts import unavailable_admission_result
from app.conversation.receipts import CapabilityReceipt
from app.domain.job_scenario import JobScenario
from app.schemas.job import CreateWritingJobRequest, WritingJobResponse
from app.services.job import JobNotFoundError, JobService
from app.services.outbox import OutboxService


def project_writing_job(job) -> dict:
    view = WritingJobResponse.model_validate(job).model_dump(mode="json")
    # Identity/requirements are not needed by the planner or its receipt cards.
    view.pop("tenant_id")
    view.pop("requirements")
    view["topic"] = view["topic"][:500]
    view["version"] = str(job.version)
    gate = {"research_review": "research", "outline_review": "outline",
            "final_review": "final"}.get(view["status"])
    if view["status"] == "waiting_human":
        gate = {"research": "research", "outline": "outline", "review": "review",
                "finalize": "final"}.get(view["current_step"], "human")
    view["pending_gate"] = gate
    return view


class ConversationWritingReader:
    def __init__(self, *, database, jobs: JobService | None = None, admission=None):
        self.database = database
        self.jobs = jobs or JobService()
        self.admission = admission

    async def _admission(self, tenant_id, job):
        if self.admission is None:
            return None
        try:
            async with asyncio.timeout(2):
                result = await self.admission.status(tenant_id=tenant_id, business_workflow_id=job.temporal_workflow_id)
            return None if result.get("not_found") else result
        except Exception:
            # The caller has already read this job through tenant-scoped SQL.
            return unavailable_admission_result(tenant_id, job.temporal_workflow_id)

    async def list_jobs(self, *, tenant_id: str, limit: int) -> dict:
        async with self.database.session() as session:
            jobs, total = await self.jobs.list_for_operations(
                session, tenant_id=UUID(tenant_id), limit=limit, offset=0,
            )
            items = []
            for job in jobs:
                view = project_writing_job(job)
                admission = await self._admission(tenant_id, job)
                if admission is not None:
                    view.update({key: admission[key] for key in ("admission", "admission_accepted", "workflow_accepted")})
                items.append(view)
            return {"items": items, "total": total,
                    "snapshot_at": datetime.now(timezone.utc).isoformat()}

    async def job_status(self, *, tenant_id: str, job_id: str) -> dict:
        async with self.database.session() as session:
            try:
                job = await self.jobs.get(session, tenant_id=UUID(tenant_id), job_id=UUID(job_id))
            except JobNotFoundError:
                return {"not_found": True, "snapshot_at": datetime.now(timezone.utc).isoformat()}
            admission = await self._admission(tenant_id, job)
            result = {"job": project_writing_job(job), "snapshot_at": datetime.now(timezone.utc).isoformat()}
            if admission is not None:
                result.update({key: admission[key] for key in ("admission", "admission_accepted", "workflow_accepted")})
                result["job"].update({key: result[key] for key in ("admission", "admission_accepted", "workflow_accepted")})
            return result


class ConversationWritingConflict(ValueError):
    """The same conversation request already binds different writing parameters."""


class _WritingProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    topic: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    target_words: int = Field(default=1000, ge=200, le=5000)
    tone: Literal["neutral", "explanatory", "concise"] = "neutral"


class ConversationWritingCommands:
    """Start an explicitly confirmed proposal and return only Workflow acceptance.

    The caller owns confirmation/permissions and cancellation checkpointing.
    This adapter commits the existing job before binding its receipt or issuing
    the Temporal start, so an interrupted caller retains an inspectable job.
    """

    def __init__(self, *, database, orchestrator, jobs: JobService | None = None, admission=None):
        self.database = database
        self.jobs = jobs or JobService(OutboxService())
        self.orchestrator = orchestrator
        self.admission = admission

    async def start(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID | str,
        request_id: UUID | str,
        proposal: dict,
        original_content: str,
        on_bound: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict:
        suggested = _WritingProposal.model_validate(proposal)
        conversation = UUID(str(conversation_id))
        request_uuid = UUID(str(request_id))
        request = CreateWritingJobRequest(
            topic=suggested.topic,
            scenario=JobScenario.ASSISTED_WRITING,
            requirements={
                "target_words": suggested.target_words,
                "tone": suggested.tone,
                "brief": original_content[:1000],
            },
            idempotency_key=f"conversation-writing:{conversation}:{request_uuid}",
        )
        async with self.database.session() as session:
            job, created = await self.jobs.create_or_get(
                session,
                tenant_id=UUID(tenant_id),
                created_by=UUID(user_id),
                request=request,
            )
            if not created and (
                job.topic != request.topic
                or job.scenario != request.scenario
                or job.requirements != request.requirements
            ):
                raise ConversationWritingConflict("writing_request_conflict")

            await session.commit()
            snapshot_at = datetime.now(timezone.utc)
            projected = project_writing_job(job)
            metadata = {
                "job_id": str(job.id),
                "workflow_id": job.temporal_workflow_id,
                "job": projected,
                "receipt": CapabilityReceipt(
                    entity_type="writing_job",
                    entity_id=str(job.id),
                    status="pending_approval" if projected["pending_gate"] else "landed",
                    version=projected["version"],
                    deep_link=f"/jobs?job_id={job.id}",
                    created_at=snapshot_at,
                    pending_gate=projected["pending_gate"],
                ).model_dump(mode="json"),
                "snapshot_at": snapshot_at.isoformat(),
                "workflow_accepted": False,
            }
            if on_bound is not None:
                await on_bound(metadata)
            if self.admission is not None:
                accepted = await self.admission.start_writing(job)
                if accepted["workflow_id"] != job.temporal_workflow_id:
                    raise ConversationWritingConflict("writing_admission_identity_mismatch")
                result = {**metadata, **{key: accepted[key] for key in
                    ("workflow_accepted", "admission_accepted", "admission")}}
                result["job"] = {**projected, **{key: result[key] for key in
                    ("workflow_accepted", "admission_accepted", "admission")}}
                if on_bound is not None:
                    await on_bound(result)
                if not result["admission_accepted"]:
                    raise ConversationWritingConflict(result["admission"].get("error_code") or "admission_rejected")
                return result
            await self.orchestrator.start_job(job)
            return {**metadata, "workflow_accepted": True}
