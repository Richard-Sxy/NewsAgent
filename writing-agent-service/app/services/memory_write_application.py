"""短期记忆与长期候选的幂等写入 Application Service。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.user_memory import PostgresUserMemoryRepository
from app.schemas.user_memory import (
    CreateShortTermMemoryCommand,
    LongTermMemoryCandidate,
    ProposeLongTermMemoryCommand,
    ShortTermUserMemory,
)


class MemoryWriteConflictError(RuntimeError):
    """幂等键对应了不同内容，或发生唯一键并发冲突。"""


class MemoryWritePersistenceError(RuntimeError):
    """Memory 写入发生可重试的数据库错误。"""

    retryable = True


@dataclass(frozen=True, slots=True)
class ShortTermMemoryWriteOutcome:
    memory: ShortTermUserMemory
    created: bool


@dataclass(frozen=True, slots=True)
class MemoryCandidateWriteOutcome:
    candidate: LongTermMemoryCandidate
    created: bool


class MemoryWriteApplicationService:
    """唯一允许创建短期记忆和长期候选的应用层入口。"""

    async def create_short_term(
        self,
        session: AsyncSession,
        *,
        command: CreateShortTermMemoryCommand,
        now: datetime,
        memory_id: UUID | None = None,
    ) -> ShortTermMemoryWriteOutcome:
        self._validate_now(now)
        memory = ShortTermUserMemory(
            id=memory_id or uuid4(),
            scope=command.scope,
            task_id=command.task_id,
            content=command.content,
            origin=command.origin,
            source_refs=command.source_refs,
            confidence=command.confidence,
            created_at=now,
            expires_at=command.expires_at,
        )
        repository = PostgresUserMemoryRepository(session)

        try:
            existing = await repository.get_short_term_by_idempotency_key(
                tenant_id=command.scope.tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                self._ensure_same_short_term(existing, command)
                return ShortTermMemoryWriteOutcome(existing, False)

            inserted = await repository.insert_short_term_memory(
                memory=memory,
                idempotency_key=command.idempotency_key,
            )
            if inserted:
                return ShortTermMemoryWriteOutcome(memory, True)

            existing = await repository.get_short_term_by_idempotency_key(
                tenant_id=command.scope.tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is None:
                raise MemoryWritePersistenceError(
                    "短期记忆写入冲突，但无法读取已有记录"
                )
            self._ensure_same_short_term(existing, command)
            return ShortTermMemoryWriteOutcome(existing, False)
        except MemoryWriteConflictError:
            raise
        except IntegrityError as exc:
            raise MemoryWriteConflictError(
                "短期记忆写入发生数据库约束冲突"
            ) from exc
        except SQLAlchemyError as exc:
            raise MemoryWritePersistenceError(
                "短期记忆持久化失败"
            ) from exc

    async def propose_long_term(
        self,
        session: AsyncSession,
        *,
        command: ProposeLongTermMemoryCommand,
        now: datetime,
        candidate_id: UUID | None = None,
    ) -> MemoryCandidateWriteOutcome:
        self._validate_now(now)
        candidate = LongTermMemoryCandidate(
            id=candidate_id or uuid4(),
            scope=command.scope,
            proposed_content=command.proposed_content,
            origin=command.origin,
            source_refs=command.source_refs,
            confidence=command.confidence,
            reason=command.reason,
            created_at=now,
            expires_at=command.expires_at,
        )
        repository = PostgresUserMemoryRepository(session)

        try:
            existing = await repository.get_candidate_by_idempotency_key(
                tenant_id=command.scope.tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                self._ensure_same_candidate(existing, command)
                return MemoryCandidateWriteOutcome(existing, False)

            inserted = await repository.insert_memory_candidate(
                candidate=candidate,
                idempotency_key=command.idempotency_key,
            )
            if inserted:
                return MemoryCandidateWriteOutcome(candidate, True)

            existing = await repository.get_candidate_by_idempotency_key(
                tenant_id=command.scope.tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is None:
                raise MemoryWritePersistenceError(
                    "长期记忆候选写入冲突，但无法读取已有记录"
                )
            self._ensure_same_candidate(existing, command)
            return MemoryCandidateWriteOutcome(existing, False)
        except MemoryWriteConflictError:
            raise
        except IntegrityError as exc:
            raise MemoryWriteConflictError(
                "长期记忆候选写入发生数据库约束冲突"
            ) from exc
        except SQLAlchemyError as exc:
            raise MemoryWritePersistenceError(
                "长期记忆候选持久化失败"
            ) from exc

    @staticmethod
    def _ensure_same_short_term(
        existing: ShortTermUserMemory,
        command: CreateShortTermMemoryCommand,
    ) -> None:
        if (
            existing.scope != command.scope
            or existing.task_id != command.task_id
            or existing.content != command.content
            or existing.origin != command.origin
            or existing.source_refs != command.source_refs
            or existing.confidence != command.confidence
            or existing.expires_at != command.expires_at
        ):
            raise MemoryWriteConflictError(
                "同一个 idempotency_key 对应不同的短期记忆内容"
            )

    @staticmethod
    def _ensure_same_candidate(
        existing: LongTermMemoryCandidate,
        command: ProposeLongTermMemoryCommand,
    ) -> None:
        if (
            existing.scope != command.scope
            or existing.proposed_content != command.proposed_content
            or existing.origin != command.origin
            or existing.source_refs != command.source_refs
            or existing.confidence != command.confidence
            or existing.reason != command.reason
            or existing.expires_at != command.expires_at
        ):
            raise MemoryWriteConflictError(
                "同一个 idempotency_key 对应不同的长期候选内容"
            )

    @staticmethod
    def _validate_now(now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
