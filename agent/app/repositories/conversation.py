"""聊天持久化。创建和查询会话、占用聊天轮次、保存工具检查点与最终答复，处理请求幂等和过期运行。按租户与用户隔离。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.models.conversation import ConversationRecord, ConversationTurnRecord
from app.conversation.context import ContextCursor, ConversationMemory
from app.schemas.conversation import (
    AssistantContent,
    ConversationDetailResponse,
    ConversationTurnView,
    ConversationView,
    CreateConversationRequest,
    SendConversationMessageRequest,
    ToolTrace,
)


class ConversationNotFound(Exception):
    """会话或轮次不存在于当前可信身份的作用域。"""


class ConversationConflict(Exception):
    """请求重用冲突、已有活跃执行或迟到提交。"""


@dataclass(frozen=True)
class TurnClaim:
    turn: ConversationTurnView
    acquired: bool


class PostgresConversationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, tenant_id: str, user_id: str, title: str) -> ConversationView:
        title = CreateConversationRequest(title=title).title
        now = datetime.now(timezone.utc)
        async with self._database.session() as session:
            conversation = ConversationRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                title=title,
                context_memory={},
                created_at=now,
                updated_at=now,
            )
            session.add(conversation)
            await session.flush()
            return ConversationView.model_validate(conversation)

    async def list(
        self, tenant_id: str, user_id: str, limit: int = 20
    ) -> list[ConversationView]:
        self._validate_limit(limit)
        async with self._database.session() as session:
            result = await session.execute(
                select(ConversationRecord)
                .where(
                    ConversationRecord.tenant_id == tenant_id,
                    ConversationRecord.user_id == user_id,
                    ConversationRecord.deleted_at.is_(None),
                )
                .order_by(
                    ConversationRecord.updated_at.desc(),
                    ConversationRecord.id.desc(),
                )
                .limit(limit)
            )
            return [
                ConversationView.model_validate(record)
                for record in result.scalars().all()
            ]

    async def delete(
        self,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID,
        *,
        stale_after_seconds: int = 90,
    ) -> None:
        """Hide an owned conversation without removing its history or tools.

        The same parent row lock used by begin/checkpoint/finish makes deletion
        atomic with a new turn claim. Live work is refused; expired work keeps
        its audit trail and is interrupted before the conversation is hidden.
        """
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        async with self._database.session() as session:
            conversation = await self._get_conversation(
                session, tenant_id, user_id, conversation_id,
                for_update=True, include_deleted=True,
            )
            if conversation is None:
                raise ConversationNotFound("conversation not found")
            if conversation.deleted_at is not None:
                return
            result = await session.execute(
                self._turn_query(tenant_id, user_id, conversation_id).where(
                    ConversationTurnRecord.status == "processing"
                )
            )
            active = result.scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if active is not None:
                if active.created_at > now - timedelta(seconds=stale_after_seconds):
                    raise ConversationConflict("conversation has a processing turn")
                self._interrupt(active, now)
            conversation.deleted_at = now
            conversation.updated_at = now
            await session.flush()

    async def restore(
        self, tenant_id: str, user_id: str, conversation_id: UUID,
    ) -> ConversationView:
        """Undo only the owned conversation's visibility flag; never rerun work."""
        async with self._database.session() as session:
            conversation = await self._get_conversation(
                session, tenant_id, user_id, conversation_id,
                for_update=True, include_deleted=True,
            )
            if conversation is None:
                raise ConversationNotFound("conversation not found")
            if conversation.deleted_at is not None:
                conversation.deleted_at = None
                conversation.updated_at = datetime.now(timezone.utc)
                await session.flush()
            return ConversationView.model_validate(conversation)

    async def get(
        self,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID,
        limit: int = 50,
        stale_after_seconds: int | None = None,
    ) -> ConversationDetailResponse | None:
        """Read recent turns; an explicit deadline also recovers interrupted work."""
        self._validate_limit(limit)
        if stale_after_seconds is not None and stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        async with self._database.session() as session:
            conversation = await self._get_conversation(
                session,
                tenant_id,
                user_id,
                conversation_id,
                for_update=stale_after_seconds is not None,
            )
            if conversation is None:
                return None
            if stale_after_seconds is not None:
                active_result = await session.execute(
                    self._turn_query(tenant_id, user_id, conversation_id).where(
                        ConversationTurnRecord.status == "processing"
                    )
                )
                active = active_result.scalar_one_or_none()
                now = datetime.now(timezone.utc)
                stale_before = now - timedelta(seconds=stale_after_seconds)
                if active is not None and active.created_at <= stale_before:
                    self._interrupt(active, now)
                    conversation.updated_at = now
                    await session.flush()
            result = await session.execute(
                self._turn_query(tenant_id, user_id, conversation_id)
                .order_by(
                    ConversationTurnRecord.created_at.desc(),
                    ConversationTurnRecord.id.desc(),
                )
                .limit(limit)
            )
            records = result.scalars().all()
            return ConversationDetailResponse(
                conversation=ConversationView.model_validate(conversation),
                turns=[
                    ConversationTurnView.model_validate(record)
                    for record in reversed(records)
                ],
            )

    async def load_memory(self, tenant_id, user_id, conversation_id) -> ConversationMemory:
        async with self._database.session() as session:
            conversation = await self._get_conversation(
                session, tenant_id, user_id, conversation_id, for_update=False
            )
            if conversation is None:
                raise ConversationNotFound("conversation not found")
            return ConversationMemory.model_validate(conversation.context_memory)

    async def context_page(self, tenant_id, user_id, conversation_id, *,
                           after: ContextCursor | None, before: ContextCursor, limit=100):
        """Keyset pagination of completed turns, independent of UI history limits."""
        self._validate_limit(limit)
        async with self._database.session() as session:
            conversation = await self._get_conversation(
                session, tenant_id, user_id, conversation_id, for_update=False
            )
            if conversation is None:
                raise ConversationNotFound("conversation not found")
            key = tuple_(ConversationTurnRecord.created_at, ConversationTurnRecord.id)
            query = self._turn_query(tenant_id, user_id, conversation_id).where(
                ConversationTurnRecord.status == "completed",
                key < tuple_(before.created_at, before.id),
            )
            if after is not None:
                query = query.where(key > tuple_(after.created_at, after.id))
            result = await session.execute(query.order_by(
                ConversationTurnRecord.created_at, ConversationTurnRecord.id
            ).limit(limit))
            return [ConversationTurnView.model_validate(item) for item in result.scalars().all()]

    async def save_memory(self, *, tenant_id, user_id, conversation_id, turn_id, request_id,
                          expected_through, memory: ConversationMemory, tools, model_request_ids):
        async with self._database.session() as session:
            conversation = await self._require_conversation(session, tenant_id, user_id, conversation_id)
            previous = ConversationMemory.model_validate(conversation.context_memory)
            if previous.through != expected_through:
                raise ConversationConflict("conversation summary changed")
            result = await session.execute(self._turn_query(tenant_id, user_id, conversation_id).where(
                ConversationTurnRecord.id == turn_id, ConversationTurnRecord.request_id == request_id
            ))
            active = result.scalar_one_or_none()
            if active is None:
                raise ConversationNotFound("conversation request claim not found")
            if active.status != "processing":
                raise ConversationConflict("only a processing request can compact memory")
            if (memory.through is None or
                (expected_through is not None and memory.through.key() <= expected_through.key()) or
                memory.through.key() >= (active.created_at, active.id)):
                raise ConversationConflict("invalid summary watermark")
            covered = await session.execute(self._turn_query(tenant_id, user_id, conversation_id).where(
                ConversationTurnRecord.id == memory.through.id,
                ConversationTurnRecord.created_at == memory.through.created_at,
                ConversationTurnRecord.status == "completed",
            ))
            if covered.scalar_one_or_none() is None:
                raise ConversationConflict("summary watermark must refer to a completed owned turn")
            serialized = [ToolTrace.model_validate(item).model_dump(mode="json") for item in tools]
            self._validate_checkpoint_prefix(active, serialized, model_request_ids)
            active.tools = serialized
            active.model_request_ids = list(model_request_ids)
            conversation.context_memory = memory.model_dump(mode="json")
            conversation.updated_at = datetime.now(timezone.utc)
            await session.flush()

    async def begin_turn(
        self,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID,
        request_id: UUID,
        content: str,
        stale_after_seconds: int = 90,
        runtime_metadata: dict[str, Any] | None = None,
    ) -> TurnClaim:
        content = SendConversationMessageRequest(
            request_id=request_id, content=content
        ).content
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        async with self._database.session() as session:
            conversation = await self._require_conversation(
                session, tenant_id, user_id, conversation_id
            )
            now = datetime.now(timezone.utc)
            stale_before = now - timedelta(seconds=stale_after_seconds)
            query = self._turn_query(tenant_id, user_id, conversation_id)
            result = await session.execute(
                query.where(ConversationTurnRecord.request_id == request_id)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                if existing.user_content != content:
                    raise ConversationConflict(
                        "request_id already belongs to different content"
                    )
                if existing.status == "processing" and existing.created_at <= stale_before:
                    self._interrupt(existing, now)
                    conversation.updated_at = now
                    await session.flush()
                return TurnClaim(ConversationTurnView.model_validate(existing), acquired=False)

            active_result = await session.execute(
                query.where(ConversationTurnRecord.status == "processing")
            )
            active = active_result.scalar_one_or_none()
            if active is not None:
                if active.created_at > stale_before:
                    raise ConversationConflict("conversation already has a processing turn")
                self._interrupt(active, now)
                # Flush the expired record before inserting a new processing row,
                # so the partial unique index never observes two active turns.
                await session.flush()
            turn = ConversationTurnRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                request_id=request_id,
                user_content=content,
                assistant_content=None,
                status="processing",
                tools=[],
                model_request_ids=[],
                error_code=None,
                runtime_metadata=deepcopy(runtime_metadata or {}),
                created_at=now,
                completed_at=None,
            )
            session.add(turn)
            conversation.updated_at = now
            await session.flush()
            return TurnClaim(ConversationTurnView.model_validate(turn), acquired=True)

    async def checkpoint_turn(
        self,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        request_id: UUID,
        tools: list[ToolTrace],
        model_request_ids: list[str],
    ) -> ConversationTurnView:
        """提交终端工具跟踪，同时保留原始请求声明。

        拥有的会话锁通过过期恢复来序列化检查点
        和最终确定。现有的跟踪/模型 ID 前缀无法重写。
        这不会重新获取中断的工作或恢复工具执行。
        """
        serialized_tools = [
            ToolTrace.model_validate(trace).model_dump(mode="json") for trace in tools
        ]
        async with self._database.session() as session:
            conversation = await self._require_conversation(
                session, tenant_id, user_id, conversation_id
            )
            result = await session.execute(
                self._turn_query(tenant_id, user_id, conversation_id).where(
                    ConversationTurnRecord.id == turn_id,
                    ConversationTurnRecord.request_id == request_id,
                )
            )
            turn = result.scalar_one_or_none()
            if turn is None:
                raise ConversationNotFound("conversation request claim not found")
            if turn.status != "processing":
                raise ConversationConflict("only a processing turn can be checkpointed")
            self._validate_checkpoint_prefix(turn, serialized_tools, model_request_ids)
            turn.tools = serialized_tools
            turn.model_request_ids = list(model_request_ids)
            conversation.updated_at = datetime.now(timezone.utc)
            await session.flush()
            return ConversationTurnView.model_validate(turn)

    """保存工具链路，用户和会话信息，工具调用记录等内容。"""
    async def finish_turn(
        self,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        assistant_content: str | None,
        tools: list[ToolTrace],
        model_request_ids: list[str],
        error_code: str | None = None,
    ) -> ConversationTurnView:
        assistant_content = TypeAdapter(AssistantContent | None).validate_python(
            assistant_content
        )
        serialized_tools = [
            ToolTrace.model_validate(trace).model_dump(mode="json")
            for trace in tools
        ]
        # 这边取数据库调用内容
        async with self._database.session() as session:
            conversation = await self._require_conversation(
                session, tenant_id, user_id, conversation_id
            )
            result = await session.execute(
                self._turn_query(tenant_id, user_id, conversation_id).where(
                    ConversationTurnRecord.id == turn_id
                )
            )
            turn = result.scalar_one_or_none()
            if turn is None:
                raise ConversationNotFound("conversation turn not found")
            if turn.status != "processing":
                raise ConversationConflict("only a processing turn can be finished")
            self._validate_checkpoint_prefix(turn, serialized_tools, model_request_ids)
            now = datetime.now(timezone.utc)
            turn.assistant_content = assistant_content
            turn.tools = serialized_tools
            turn.model_request_ids = list(model_request_ids)
            turn.error_code = error_code
            turn.status = "failed" if error_code is not None else "completed"
            turn.completed_at = now
            conversation.updated_at = now
            await session.flush()
            return ConversationTurnView.model_validate(turn)

    @staticmethod
    def _validate_checkpoint_prefix(
        turn: ConversationTurnRecord,
        tools: list[dict[str, Any]],
        model_request_ids: list[str],
    ) -> None:
        if len(tools) < len(turn.tools) or tools[:len(turn.tools)] != turn.tools:
            raise ConversationConflict("tool checkpoint cannot be shortened or rewritten")
        if (
            len(model_request_ids) < len(turn.model_request_ids)
            or model_request_ids[:len(turn.model_request_ids)] != turn.model_request_ids
        ):
            raise ConversationConflict("model request checkpoint cannot be shortened or rewritten")

    async def _require_conversation(
        self,
        session: AsyncSession,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID,
    ) -> ConversationRecord:
        conversation = await self._get_conversation(
            session, tenant_id, user_id, conversation_id, for_update=True
        )
        # 这边加一层异常报错
        if conversation is None:
            raise ConversationNotFound("conversation not found")
        return conversation

    @staticmethod
    async def _get_conversation(
        session: AsyncSession,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID,
        *,
        for_update: bool,
        include_deleted: bool = False,
    ) -> ConversationRecord | None:
        # 从 PostgreSQL 当中获取 Conversation 对话记录
        query = select(ConversationRecord).where(
            ConversationRecord.tenant_id == tenant_id,
            ConversationRecord.user_id == user_id,
            ConversationRecord.id == conversation_id,
        )
        if not include_deleted:
            query = query.where(ConversationRecord.deleted_at.is_(None))
        if for_update:
            query = query.with_for_update()
        result = await session.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    def _turn_query(tenant_id: str, user_id: str, conversation_id: UUID):
        return select(ConversationTurnRecord).where(
            ConversationTurnRecord.tenant_id == tenant_id,
            ConversationTurnRecord.user_id == user_id,
            ConversationTurnRecord.conversation_id == conversation_id,
        )

    @staticmethod
    def _interrupt(turn: ConversationTurnRecord, now: datetime) -> None:
        turn.status = "failed"
        turn.error_code = "interrupted"
        turn.completed_at = now

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
