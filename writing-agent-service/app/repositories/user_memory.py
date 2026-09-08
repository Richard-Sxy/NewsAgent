"""用户记忆的 PostgreSQL Repository。"""
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_memory import (
    LongTermMemoryCandidateRecord,
    LongTermUserMemoryRecord,
    MemoryPromotionRequestRecord,
    ShortTermUserMemoryRecord,
)
from app.schemas.user_memory import (
    LongTermMemoryCandidate,
    LongTermUserMemory,
    PromoteMemoryCandidateCommand,
    ShortTermUserMemory,
)


class UserMemoryDataCorruptedError(RuntimeError):
    """数据库中的记忆记录不能通过领域 Schema 校验。"""


class PostgresUserMemoryRepository:
    """用户记忆领域对象与 PostgreSQL ORM 之间的转换入口。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active_short_term(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
        now: datetime,
    ) -> list[ShortTermUserMemory]:
        """读取某个用户、某个任务当前有效的短期记忆"""
        statement = (
            select(ShortTermUserMemoryRecord)
            .where(
                ShortTermUserMemoryRecord.tenant_id == tenant_id,
                ShortTermUserMemoryRecord.user_id == user_id,
                ShortTermUserMemoryRecord.task_id == task_id,
                ShortTermUserMemoryRecord.status == "active",
                ShortTermUserMemoryRecord.created_at <= now,
                ShortTermUserMemoryRecord.expires_at > now,
            )
            .order_by(
                ShortTermUserMemoryRecord.created_at,
                ShortTermUserMemoryRecord.id,
            )
        )

        result = await self._session.execute(statement)
        records = result.scalars().all()

        return [
            self.to_short_term_domain(record)
            for record in records
        ]

    async def list_active_long_term(
        self,
        *,
        tenant_id: str,
        user_id: str,
        now: datetime,
    ) -> list[LongTermUserMemory]:
        """读取某个用户当前有效的长期记忆。"""

        statement = (
            select(LongTermUserMemoryRecord)
            .where(
                LongTermUserMemoryRecord.tenant_id == tenant_id,
                LongTermUserMemoryRecord.user_id == user_id,
                LongTermUserMemoryRecord.status == "active",
                LongTermUserMemoryRecord.valid_from <= now,
                or_(
                    LongTermUserMemoryRecord.valid_until.is_(None),
                    LongTermUserMemoryRecord.valid_until > now,
                ),
            )
            .order_by(
                LongTermUserMemoryRecord.valid_from,
                LongTermUserMemoryRecord.id,
            )
        )

        result = await self._session.execute(statement)
        records = result.scalars().all()

        return [
            self.to_long_term_domain(record)
            for record in records
        ]

    async def get_candidate_for_update(
        self,
        *,
        tenant_id: str,
        user_id: str,
        candidate_id: UUID,
    ) -> LongTermMemoryCandidate | None:
        """读取并锁定一个长期记忆候选。"""

        statement = (
            select(LongTermMemoryCandidateRecord)
            .where(
                LongTermMemoryCandidateRecord.tenant_id == tenant_id,
                LongTermMemoryCandidateRecord.user_id == user_id,
                LongTermMemoryCandidateRecord.id == candidate_id,
            )
            .with_for_update()
        )

        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()

        if record is None:
            return None

        return self.to_candidate_domain(record)

    async def get_long_term_for_update(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_id: UUID,
    ) -> LongTermUserMemory | None:
        """读取并锁定一条可能被替代的长期记忆。"""

        statement = (
            select(LongTermUserMemoryRecord)
            .where(
                LongTermUserMemoryRecord.tenant_id == tenant_id,
                LongTermUserMemoryRecord.user_id == user_id,
                LongTermUserMemoryRecord.id == memory_id,
            )
            .with_for_update()
        )

        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()

        if record is None:
            return None

        return self.to_long_term_domain(record)

    async def get_promotion_request_for_update(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> MemoryPromotionRequestRecord | None:
        """按租户和幂等键读取并锁定一次晋升请求。"""

        statement = (
            select(MemoryPromotionRequestRecord)
            .where(
                MemoryPromotionRequestRecord.tenant_id == tenant_id,
                MemoryPromotionRequestRecord.idempotency_key
                == idempotency_key,
            )
            .with_for_update()
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def reserve_promotion_request(
        self,
        *,
        command: PromoteMemoryCandidateCommand,
        request_fingerprint: str,
    ) -> MemoryPromotionRequestRecord | None:
        """尝试占用租户内幂等键；键已存在时返回 ``None``。"""

        statement = (
            insert(MemoryPromotionRequestRecord)
            .values(
                id=uuid4(),
                tenant_id=command.tenant_id,
                user_id=command.user_id,
                idempotency_key=command.idempotency_key,
                request_fingerprint=request_fingerprint,
                candidate_id=command.candidate_id,
                expected_candidate_version=command.expected_version,
                approved_by=command.approved_by,
                valid_until=command.valid_until,
                supersedes_memory_id=command.supersedes_memory_id,
                resulting_memory_id=None,
                status="reserved",
            )
            .on_conflict_do_nothing(
                constraint=(
                    "uq_memory_promotion_requests_tenant_idempotency_key"
                )
            )
            .returning(MemoryPromotionRequestRecord)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def mark_candidate_approved(
        self,
        *,
        candidate: LongTermMemoryCandidate,
    ) -> bool:
        """按旧版本条件批准候选，避免并发请求重复晋升。"""

        previous_version = candidate.version - 1
        statement = (
            update(LongTermMemoryCandidateRecord)
            .where(
                LongTermMemoryCandidateRecord.tenant_id
                == candidate.scope.tenant_id,
                LongTermMemoryCandidateRecord.user_id
                == candidate.scope.user_id,
                LongTermMemoryCandidateRecord.id == candidate.id,
                LongTermMemoryCandidateRecord.status == "pending",
                LongTermMemoryCandidateRecord.version == previous_version,
            )
            .values(
                status=candidate.status,
                version=candidate.version,
            )
        )
        result = await self._session.execute(statement)
        return result.rowcount == 1

    async def mark_memory_superseded(
        self,
        *,
        memory: LongTermUserMemory,
        superseded_at: datetime,
    ) -> bool:
        """将仍为 active 的指定长期记忆标记为已替代。"""

        statement = (
            update(LongTermUserMemoryRecord)
            .where(
                LongTermUserMemoryRecord.tenant_id
                == memory.scope.tenant_id,
                LongTermUserMemoryRecord.user_id
                == memory.scope.user_id,
                LongTermUserMemoryRecord.id == memory.id,
                LongTermUserMemoryRecord.status == "active",
            )
            .values(
                status="superseded",
                valid_until=superseded_at,
            )
        )
        result = await self._session.execute(statement)
        return result.rowcount == 1

    async def insert_long_term_memory(
        self,
        *,
        memory: LongTermUserMemory,
        candidate_id: UUID,
    ) -> None:
        """写入已批准长期记忆，但不提交外围事务。"""

        values = self.to_long_term_values(
            memory,
            candidate_id=candidate_id,
        )
        self._session.add(LongTermUserMemoryRecord(**values))
        await self._session.flush()

    async def complete_promotion_request(
        self,
        *,
        request_id: UUID,
        tenant_id: str,
        memory_id: UUID,
    ) -> bool:
        """将 reserved 晋升请求原子更新为 completed。"""

        statement = (
            update(MemoryPromotionRequestRecord)
            .where(
                MemoryPromotionRequestRecord.id == request_id,
                MemoryPromotionRequestRecord.tenant_id == tenant_id,
                MemoryPromotionRequestRecord.status == "reserved",
            )
            .values(
                status="completed",
                resulting_memory_id=memory_id,
            )
        )
        result = await self._session.execute(statement)
        return result.rowcount == 1

    async def get_long_term(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_id: UUID,
    ) -> LongTermUserMemory | None:
        """按租户、用户和主键读取长期记忆，用于幂等重放。"""

        statement = select(LongTermUserMemoryRecord).where(
            LongTermUserMemoryRecord.tenant_id == tenant_id,
            LongTermUserMemoryRecord.user_id == user_id,
            LongTermUserMemoryRecord.id == memory_id,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self.to_long_term_domain(record)

    @staticmethod
    def to_short_term_domain(
        record: ShortTermUserMemoryRecord,
    ) -> ShortTermUserMemory:
        """将短期记忆 ORM 记录转换为领域对象。"""

        try:
            return ShortTermUserMemory.model_validate(
                {
                    "id": record.id,
                    "scope": {
                        "tenant_id": record.tenant_id,
                        "user_id": record.user_id,
                        "team_id": record.team_id,
                        "section_id": record.section_id,
                        "role_id": record.role_id,
                    },
                    "task_id": record.task_id,
                    "content": {
                        "key": record.memory_key,
                        "kind": record.memory_kind,
                        "value": record.memory_value,
                        "summary": record.summary,
                    },
                    "origin": record.origin,
                    "source_refs": record.source_refs,
                    "confidence": record.confidence,
                    "status": record.status,
                    "created_at": record.created_at,
                    "expires_at": record.expires_at,
                    "version": record.version,
                }
            )
        except ValidationError as exc:
            raise UserMemoryDataCorruptedError(
                f"短期记忆记录格式错误：{record.id}"
            ) from exc

    @staticmethod
    def to_short_term_values(
        memory: ShortTermUserMemory,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """将短期记忆领域对象转换为 ORM 写入参数。"""

        return {
            "id": memory.id,
            **PostgresUserMemoryRepository._scope_values(memory.scope),
            "task_id": memory.task_id,
            "memory_key": memory.content.key,
            "memory_kind": memory.content.kind,
            "memory_value": memory.content.value,
            "summary": memory.content.summary,
            "origin": memory.origin,
            "source_refs": [
                source.model_dump(mode="json")
                for source in memory.source_refs
            ],
            "confidence": memory.confidence,
            "status": memory.status,
            "created_at": memory.created_at,
            "expires_at": memory.expires_at,
            "version": memory.version,
            "idempotency_key": idempotency_key,
        }

    @staticmethod
    def to_candidate_domain(
        record: LongTermMemoryCandidateRecord,
    ) -> LongTermMemoryCandidate:
        """将长期记忆候选 ORM 记录转换为领域对象。"""

        try:
            return LongTermMemoryCandidate.model_validate(
                {
                    "id": record.id,
                    "scope": {
                        "tenant_id": record.tenant_id,
                        "user_id": record.user_id,
                        "team_id": record.team_id,
                        "section_id": record.section_id,
                        "role_id": record.role_id,
                    },
                    "proposed_content": {
                        "key": record.proposed_memory_key,
                        "kind": record.proposed_memory_kind,
                        "value": record.proposed_value,
                        "summary": record.proposed_summary,
                    },
                    "origin": record.origin,
                    "source_refs": record.source_refs,
                    "confidence": record.confidence,
                    "reason": record.reason,
                    "status": record.status,
                    "created_at": record.created_at,
                    "expires_at": record.expires_at,
                    "version": record.version,
                }
            )
        except ValidationError as exc:
            raise UserMemoryDataCorruptedError(
                f"长期记忆候选记录格式错误：{record.id}"
            ) from exc

    @staticmethod
    def to_candidate_values(
        candidate: LongTermMemoryCandidate,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """将长期记忆候选转换为 ORM 写入参数。"""

        return {
            "id": candidate.id,
            **PostgresUserMemoryRepository._scope_values(candidate.scope),
            "proposed_memory_key": candidate.proposed_content.key,
            "proposed_memory_kind": candidate.proposed_content.kind,
            "proposed_value": candidate.proposed_content.value,
            "proposed_summary": candidate.proposed_content.summary,
            "origin": candidate.origin,
            "source_refs": [
                source.model_dump(mode="json")
                for source in candidate.source_refs
            ],
            "confidence": candidate.confidence,
            "reason": candidate.reason,
            "status": candidate.status,
            "created_at": candidate.created_at,
            "expires_at": candidate.expires_at,
            "version": candidate.version,
            "idempotency_key": idempotency_key,
        }

    @staticmethod
    def to_long_term_domain(
        record: LongTermUserMemoryRecord,
    ) -> LongTermUserMemory:
        """将长期记忆 ORM 记录转换为领域对象。"""

        try:
            return LongTermUserMemory.model_validate(
                {
                    "id": record.id,
                    "scope": {
                        "tenant_id": record.tenant_id,
                        "user_id": record.user_id,
                        "team_id": record.team_id,
                        "section_id": record.section_id,
                        "role_id": record.role_id,
                    },
                    "content": {
                        "key": record.memory_key,
                        "kind": record.memory_kind,
                        "value": record.memory_value,
                        "summary": record.summary,
                    },
                    "origin": record.origin,
                    "source_refs": record.source_refs,
                    "confidence": record.confidence,
                    "status": record.status,
                    "confirmed_by": record.confirmed_by,
                    "confirmed_at": record.confirmed_at,
                    "valid_from": record.valid_from,
                    "valid_until": record.valid_until,
                    "recorded_at": record.recorded_at,
                    "supersedes_memory_id": record.supersedes_memory_id,
                    "version": record.version,
                }
            )
        except ValidationError as exc:
            raise UserMemoryDataCorruptedError(
                f"长期记忆记录格式错误：{record.id}"
            ) from exc

    @staticmethod
    def to_long_term_values(
        memory: LongTermUserMemory,
        *,
        candidate_id: UUID | None = None,
    ) -> dict[str, Any]:
        """将长期记忆转换为 ORM 写入参数。"""

        return {
            "id": memory.id,
            **PostgresUserMemoryRepository._scope_values(memory.scope),
            "memory_key": memory.content.key,
            "memory_kind": memory.content.kind,
            "memory_value": memory.content.value,
            "summary": memory.content.summary,
            "origin": memory.origin,
            "source_refs": [
                source.model_dump(mode="json")
                for source in memory.source_refs
            ],
            "confidence": memory.confidence,
            "status": memory.status,
            "candidate_id": candidate_id,
            "confirmed_by": memory.confirmed_by,
            "confirmed_at": memory.confirmed_at,
            "valid_from": memory.valid_from,
            "valid_until": memory.valid_until,
            "recorded_at": memory.recorded_at,
            "supersedes_memory_id": memory.supersedes_memory_id,
            "version": memory.version,
            "created_at": memory.recorded_at,
        }

    @staticmethod
    def _scope_values(scope: Any) -> dict[str, str | None]:
        return {
            "tenant_id": scope.tenant_id,
            "user_id": scope.user_id,
            "team_id": scope.team_id,
            "section_id": scope.section_id,
            "role_id": scope.role_id,
        }
