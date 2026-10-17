"""User-scoped conversation adapters for memory writes and snapshot reads.

The existing memory application service owns writes and idempotency checks.
Conversation controls supply identity, source references and fixed validity
windows; model arguments contain only the proposed text. No promotion service
is exposed here.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select

from app.modules.conversation.receipts import CapabilityReceipt
from app.models.conversation import ConversationTurnRecord
from app.modules.memory.persistence.user_memory import LongTermMemoryCandidateRecord, ShortTermUserMemoryRecord
from app.modules.memory.persistence.user_memory_repository import PostgresUserMemoryRepository
from app.modules.memory.contracts.user_memory import (
    CreateShortTermMemoryCommand,
    LongTermMemoryCandidate,
    MemorySourceRef,
    ProposeLongTermMemoryCommand,
    ShortTermUserMemory,
    UserMemoryContent,
    UserMemoryScope,
)
from app.modules.memory.memory_write_application import MemoryWriteApplicationService


MEMORY_POLICY_VERSION = "conversation-memory-v1"
SHORT_TERM_TTL = timedelta(hours=24)
CANDIDATE_TTL = timedelta(days=7)
ConversationMemoryKind = Literal["short_term", "long_term_candidate"]


class MemoryHookSourceNotFoundError(LookupError):
    """The original request is absent or outside the authenticated user scope."""


class MemoryHookArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    content: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]


def _snapshot(
    memory: ShortTermUserMemory | LongTermMemoryCandidate,
    *,
    kind: ConversationMemoryKind,
    created: bool | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    is_candidate = kind == "long_term_candidate"
    content = memory.proposed_content if is_candidate else memory.content
    effective_status = memory.status
    if effective_status in {"active", "pending"} and memory.expires_at <= now:
        effective_status = "expired"
    pending = is_candidate and effective_status == "pending"
    receipt = CapabilityReceipt(
        entity_type="memory",
        entity_id=str(memory.id),
        status="pending_approval" if pending else "landed",
        version=str(memory.version),
        deep_link=None,
        created_at=now,
        pending_gate="memory_promotion" if pending else None,
    )
    result = {
        "receipt": receipt.model_dump(mode="json"),
        "memory": {
            "id": str(memory.id),
            "content": content.value if isinstance(content.value, str) else content.summary,
            "expires_at": memory.expires_at.isoformat(),
            "tier": kind,
            "status": memory.status,
            "effective_status": effective_status,
            "candidate_status": memory.status if is_candidate else None,
            "version": str(memory.version),
            "not_promoted": is_candidate and memory.status != "approved",
        },
        "snapshot_at": now.isoformat(),
        "policy_version": MEMORY_POLICY_VERSION,
        "promotion_performed": False,
    }
    if created is not None:
        result["created"] = created
    return result


class ConversationMemoryHooks:
    def __init__(self, *, database, writer: MemoryWriteApplicationService | None = None):
        self.database = database
        self.writer = writer or MemoryWriteApplicationService()

    async def _source(self, session, *, tenant_id, user_id, conversation_id, request_id):
        conversation = UUID(str(conversation_id))
        request = UUID(str(request_id))
        result = await session.execute(select(ConversationTurnRecord).where(
            ConversationTurnRecord.tenant_id == tenant_id,
            ConversationTurnRecord.user_id == user_id,
            ConversationTurnRecord.conversation_id == conversation,
            ConversationTurnRecord.request_id == request,
        ))
        turn = result.scalar_one_or_none()
        if turn is None:
            raise MemoryHookSourceNotFoundError("memory_source_not_found")
        captured_at = turn.created_at
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("memory_source_timestamp_invalid")
        return conversation, request, captured_at

    async def save_short_term(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID | str,
        request_id: UUID | str, content: str,
        on_bound: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict:
        return await self._write(kind="short_term", tenant_id=tenant_id, user_id=user_id,
            conversation_id=conversation_id, request_id=request_id, content=content, on_bound=on_bound)

    async def propose_long_term(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID | str,
        request_id: UUID | str, content: str,
        on_bound: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict:
        return await self._write(kind="long_term_candidate", tenant_id=tenant_id, user_id=user_id,
            conversation_id=conversation_id, request_id=request_id, content=content, on_bound=on_bound)

    async def _write(self, *, kind, tenant_id, user_id, conversation_id, request_id, content, on_bound):
        arguments = MemoryHookArguments(content=content)
        scope = UserMemoryScope(tenant_id=tenant_id, user_id=user_id)
        async with self.database.session() as session:
            conversation, request, captured_at = await self._source(session,
                tenant_id=scope.tenant_id, user_id=scope.user_id,
                conversation_id=conversation_id, request_id=request_id)
            structured_content = UserMemoryContent(
                kind="temporary_preference" if kind == "short_term" else "stable_preference",
                key=f"conversation.note:{conversation}:{request}",
                value=arguments.content,
                summary=arguments.content[:500],
            )
            source_refs = (MemorySourceRef(source_type="user_message",
                source_id=f"conversation:{conversation}:request:{request}", captured_at=captured_at),)
            identity = f"{MEMORY_POLICY_VERSION}:{kind}:{conversation}:{request}"
            if kind == "short_term":
                outcome = await self.writer.create_short_term(session,
                    command=CreateShortTermMemoryCommand(scope=scope, task_id=str(conversation),
                        content=structured_content, origin="explicit_user", source_refs=source_refs,
                        confidence=1, expires_at=captured_at + SHORT_TERM_TTL, idempotency_key=identity),
                    now=captured_at)
                stored = outcome.memory
            else:
                outcome = await self.writer.propose_long_term(session,
                    command=ProposeLongTermMemoryCommand(scope=scope, proposed_content=structured_content,
                        origin="explicit_user", source_refs=source_refs, confidence=1,
                        reason="用户通过对话提出长期记忆候选；等待人工审批，不直接晋升。",
                        expires_at=captured_at + CANDIDATE_TTL, idempotency_key=identity),
                    now=captured_at)
                stored = outcome.candidate
            await session.commit()
            metadata = _snapshot(stored, kind=kind, created=outcome.created)
            if on_bound is not None:
                await on_bound(metadata)
            return metadata

    async def status(
        self, *, tenant_id: str, user_id: str, memory_id: UUID | str, kind: ConversationMemoryKind,
    ) -> dict:
        if kind not in {"short_term", "long_term_candidate"}:
            raise ValueError("memory_kind_invalid")
        scope = UserMemoryScope(tenant_id=tenant_id, user_id=user_id)
        model = ShortTermUserMemoryRecord if kind == "short_term" else LongTermMemoryCandidateRecord
        async with self.database.session() as session:
            result = await session.execute(select(model).where(
                model.tenant_id == scope.tenant_id,
                model.user_id == scope.user_id,
                model.id == UUID(str(memory_id)),
            ))
            row = result.scalar_one_or_none()
            if row is None:
                now = datetime.now(timezone.utc)
                return {"not_found": True, "memory": None, "snapshot_at": now.isoformat(),
                    "receipt": CapabilityReceipt(entity_type="memory", entity_id=None,
                        status="not_landed", version=None, deep_link=None,
                        created_at=now).model_dump(mode="json")}
            converter = (PostgresUserMemoryRepository.to_short_term_domain if kind == "short_term"
                         else PostgresUserMemoryRepository.to_candidate_domain)
            return _snapshot(converter(row), kind=kind)
