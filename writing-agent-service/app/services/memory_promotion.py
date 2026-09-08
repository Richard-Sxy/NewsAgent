"""长期记忆候选的审批规则与纯领域晋升服务。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from app.schemas.user_memory import (
    LongTermMemoryCandidate,
    LongTermUserMemory,
    PromoteMemoryCandidateCommand,
)


class MemoryPromotionRejectedError(ValueError):
    """候选不满足长期记忆晋升条件。"""


@dataclass(frozen=True, slots=True)
class MemoryPromotionResult:
    approved_candidate: LongTermMemoryCandidate
    long_term_memory: LongTermUserMemory
    idempotency_key: str


class MemoryPromotionPolicy:
    PROMOTABLE_KINDS = {
        "stable_preference",
        "role_responsibility",
        "user_constraint",
    }

    def validate(
        self,
        *,
        candidate: LongTermMemoryCandidate,
        command: PromoteMemoryCandidateCommand,
        now: datetime,
        superseded_memory: LongTermUserMemory | None = None,
    ) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise MemoryPromotionRejectedError("now must be timezone-aware")
        if (
            candidate.scope.tenant_id != command.tenant_id
            or candidate.scope.user_id != command.user_id
            or candidate.id != command.candidate_id
        ):
            raise MemoryPromotionRejectedError(
                "memory candidate is not available for promotion"
            )
        if candidate.version != command.expected_version:
            raise MemoryPromotionRejectedError(
                "memory candidate version conflict"
            )
        if candidate.status != "pending":
            raise MemoryPromotionRejectedError(
                "only pending memory candidates can be promoted"
            )
        if not (candidate.created_at <= now < candidate.expires_at):
            raise MemoryPromotionRejectedError(
                "memory candidate is not currently active"
            )
        if candidate.proposed_content.kind not in self.PROMOTABLE_KINDS:
            raise MemoryPromotionRejectedError(
                "memory kind is not eligible for long-term promotion"
            )
        if command.valid_until is not None and command.valid_until <= now:
            raise MemoryPromotionRejectedError(
                "valid_until must be later than promotion time"
            )
        self._validate_superseded_memory(
            candidate=candidate,
            command=command,
            superseded_memory=superseded_memory,
        )

    @staticmethod
    def _validate_superseded_memory(
        *,
        candidate: LongTermMemoryCandidate,
        command: PromoteMemoryCandidateCommand,
        superseded_memory: LongTermUserMemory | None,
    ) -> None:
        target_id = command.supersedes_memory_id
        if target_id is None:
            if superseded_memory is not None:
                raise MemoryPromotionRejectedError(
                    "unexpected superseded memory"
                )
            return
        if superseded_memory is None or superseded_memory.id != target_id:
            raise MemoryPromotionRejectedError(
                "superseded memory is not available"
            )
        if (
            superseded_memory.scope != candidate.scope
            or superseded_memory.content.key
            != candidate.proposed_content.key
            or superseded_memory.status != "active"
        ):
            raise MemoryPromotionRejectedError(
                "superseded memory is not available"
            )


class MemoryPromotionService:
    """构造需在同一数据库事务中持久化的晋升结果。"""

    def __init__(self, policy: MemoryPromotionPolicy | None = None) -> None:
        self._policy = policy or MemoryPromotionPolicy()

    def promote(
        self,
        *,
        candidate: LongTermMemoryCandidate,
        command: PromoteMemoryCandidateCommand,
        now: datetime,
        superseded_memory: LongTermUserMemory | None = None,
        memory_id: UUID | None = None,
    ) -> MemoryPromotionResult:
        self._policy.validate(
            candidate=candidate,
            command=command,
            now=now,
            superseded_memory=superseded_memory,
        )
        approved_candidate = candidate.model_copy(
            update={
                "status": "approved",
                "version": candidate.version + 1,
            },
            deep=True,
        )
        long_term_memory = LongTermUserMemory(
            id=memory_id or uuid4(),
            scope=candidate.scope,
            content=candidate.proposed_content.model_copy(deep=True),
            origin="approved_candidate",
            source_refs=candidate.source_refs,
            confidence=candidate.confidence,
            status="active",
            confirmed_by=command.approved_by,
            confirmed_at=now,
            valid_from=now,
            valid_until=command.valid_until,
            recorded_at=now,
            supersedes_memory_id=command.supersedes_memory_id,
            version=(
                1
                if superseded_memory is None
                else superseded_memory.version + 1
            ),
        )
        return MemoryPromotionResult(
            approved_candidate=approved_candidate,
            long_term_memory=long_term_memory,
            idempotency_key=command.idempotency_key,
        )
