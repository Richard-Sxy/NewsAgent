"""Confirmed conversation feedback reuses the operator decision and Case services.

This adapter records an explicitly disclosed rejection of the saved analysis.
The desired label stays in the operator's reason as a draft. Label submission,
review, approval and dataset freezing remain outside the conversation tool.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.conversation.receipts import CapabilityReceipt
from app.repositories.analysis_feedback import PostgresAnalysisFeedbackRepository
from app.schemas.analysis_feedback import FeedbackProblemType
from app.schemas.hot_news_decision import RecordHotNewsDecisionCommand
from app.schemas.hot_news_memory import HotNewsAnalysisMemory
from app.schemas.operations_feedback import OperationsFeedback
from app.services.data_loop.feedback_collector import AnalysisFeedbackPersistenceError
from app.services.hot_news_decision import HotNewsDecisionService, HotNewsDecisionTargetNotFoundError
from app.services.hot_news_run_store import PostgresHotNewsRunStore


FEEDBACK_CONFIRMATION_NOTICE = "同时记录本次分析驳回反馈；期望标签仅草稿，需在评审台标注并二审。"
DRAFT_REASON_PREFIX = "期望标签草稿（尚未复核）："


class FeedbackCaseArguments(BaseModel):
    """The model suggests an issue and draft for a previously read analysis."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
    news_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    issue_type: FeedbackProblemType = "analysis_incorrect"
    expected_label_draft: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=96)
    ]


class ConversationFeedbackHooks:
    """Thin source lookup, existing decision application, and receipt projection."""

    def __init__(self, *, database, run_store=None, decisions=None):
        self.database = database
        self.run_store = run_store or PostgresHotNewsRunStore(database)
        self.decisions = decisions or HotNewsDecisionService(
            database=database, memory_store=self.run_store
        )

    async def prepare(self, *, tenant_id: str, proposal: dict) -> dict:
        arguments = FeedbackCaseArguments.model_validate(proposal)
        memory = await self._source(tenant_id=tenant_id, arguments=arguments)
        now = datetime.now(timezone.utc)
        return {
            "suggested": arguments.model_dump(),
            "confirmation_fields": self._source_fields(memory, arguments),
            "receipt": CapabilityReceipt(
                entity_type="feedback_case", entity_id=None, status="not_landed",
                version=None, deep_link=None, created_at=now,
            ).model_dump(mode="json"),
        }

    async def start(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID | str,
        request_id: UUID | str, proposal: dict,
        on_bound: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict:
        arguments = FeedbackCaseArguments.model_validate(proposal)
        memory = await self._source(tenant_id=tenant_id, arguments=arguments)
        # Identity and the idempotency key come from the server's owned turn.
        conversation_uuid = UUID(str(conversation_id))
        request_uuid = UUID(str(request_id))
        tenant_uuid, user_uuid = UUID(tenant_id), UUID(user_id)
        command = RecordHotNewsDecisionCommand(
            run_id=memory.run_id,
            news_id=memory.news_id,
            decision_type="rejected",
            reason=DRAFT_REASON_PREFIX + arguments.expected_label_draft,
            operator_feedback=OperationsFeedback(
                category="analysis_error", reason_codes=("other_with_explanation",),
            ),
            idempotency_key=f"conversation-feedback:{conversation_uuid}:{request_uuid}",
        )
        # The existing application service commits the decision and its Case
        # together and checks conflicting replays. Never recreate that logic.
        decision, created = await self.decisions.record_decision(
            tenant_id=str(tenant_uuid), operator_id=str(user_uuid), command=command,
            feedback_problem_type=arguments.issue_type, feedback_severity="medium",
        )
        if (decision.tenant_id != str(tenant_uuid) or decision.operator_id != str(user_uuid)
                or decision.run_id != memory.run_id or decision.news_id != memory.news_id
                or decision.decision_type != "rejected"):
            raise AnalysisFeedbackPersistenceError("反馈决定与已确认来源不一致")
        async with self.database.session() as session:
            case = await PostgresAnalysisFeedbackRepository(session).get_case_by_idempotency_key(
                tenant_id=str(tenant_uuid), idempotency_key=f"feedback:operator-decision:{decision.id}",
            )
        if case is None:
            raise AnalysisFeedbackPersistenceError("运营反馈已保存，但无法读取对应案例")
        if (case.tenant_id != str(tenant_uuid) or case.run_id != memory.run_id
                or case.news_id != memory.news_id
                or case.production_bundle_version != memory.production_bundle_version
                or case.problem_type != arguments.issue_type
                or case.analysis_input_snapshot != memory.analysis_input
                or case.analysis_output_snapshot != memory.analysis_report
                or case.source_type != "operator_rejected"
                or case.source_reference.reference_type != "operator_decision"
                or case.source_reference.reference_id != str(decision.id)
                or case.source_reference.diagnostic_summary != command.reason):
            raise AnalysisFeedbackPersistenceError("反馈案例与已确认来源不一致")
        result = {
            "case_id": str(case.id), "decision_id": str(decision.id), "created": created,
            "run_id": str(case.run_id), "news_id": case.news_id,
            "production_bundle_version": case.production_bundle_version,
            "issue_type": case.problem_type, "status": case.status,
            "expected_label_draft": arguments.expected_label_draft,
            "label_submitted": False, "label_approved": False,
            "source_action": "reject_analysis", "notice": FEEDBACK_CONFIRMATION_NOTICE,
            "snapshot_at": case.recorded_at.isoformat(),
            "receipt": CapabilityReceipt(
                entity_type="feedback_case", entity_id=str(case.id),
                status="pending_approval", version=case.content_sha256,
                deep_link=f"/data-loop?case_id={case.id}",
                pending_gate="feedback_label_review", created_at=case.recorded_at,
            ).model_dump(mode="json"),
        }
        if on_bound is not None:
            await on_bound(result)
        return result

    async def _source(self, *, tenant_id: str, arguments: FeedbackCaseArguments) -> HotNewsAnalysisMemory:
        memory = await self.run_store.get_analysis_memory(
            tenant_id=tenant_id, run_id=UUID(arguments.run_id), news_id=arguments.news_id,
        )
        if (memory is None or memory.tenant_id != tenant_id
                or str(memory.run_id) != arguments.run_id or memory.news_id != arguments.news_id
                or memory.analysis_input.news_id != arguments.news_id
                or memory.analysis_report.news_id != arguments.news_id):
            raise HotNewsDecisionTargetNotFoundError("当前租户不存在该新闻的已校验分析")
        return memory

    @staticmethod
    def _source_fields(memory: HotNewsAnalysisMemory, arguments: FeedbackCaseArguments) -> dict:
        return {
            "run_id": str(memory.run_id), "news_id": memory.news_id,
            "title": memory.analysis_input.title[:500],
            "production_bundle_version": memory.production_bundle_version,
            "issue_type": arguments.issue_type,
            "expected_label_draft": arguments.expected_label_draft,
            "source_action": "reject_analysis", "notice": FEEDBACK_CONFIRMATION_NOTICE,
        }
