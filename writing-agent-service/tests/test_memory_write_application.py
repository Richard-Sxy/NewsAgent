from datetime import datetime, timedelta, timezone
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from sqlalchemy.exc import SQLAlchemyError

import app.services.memory_write_application as application_module
from app.repositories.user_memory import PostgresUserMemoryRepository
from app.schemas.user_memory import (
    CreateShortTermMemoryCommand,
    MemorySourceRef,
    ProposeLongTermMemoryCommand,
    UserMemoryContent,
    UserMemoryScope,
)
from app.services.memory_write_application import (
    MemoryWriteApplicationService,
    MemoryWriteConflictError,
    MemoryWritePersistenceError,
)


NOW = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)
EXPIRES_AT = NOW + timedelta(days=1)
SCOPE = UserMemoryScope(
    tenant_id="tenant-1",
    user_id="user-1",
    team_id="team-news",
)
SOURCE_REFS = (
    MemorySourceRef(
        source_type="user_message",
        source_id="message-1",
        captured_at=NOW,
    ),
)


def short_term_command() -> CreateShortTermMemoryCommand:
    return CreateShortTermMemoryCommand(
        scope=SCOPE,
        task_id="task-1",
        content=UserMemoryContent(
            kind="temporary_preference",
            key="output.language",
            value="zh-CN",
            summary="本次任务使用中文",
        ),
        origin="explicit_user",
        source_refs=SOURCE_REFS,
        confidence=1,
        expires_at=EXPIRES_AT,
        idempotency_key="short-memory-1",
    )


def candidate_command() -> ProposeLongTermMemoryCommand:
    return ProposeLongTermMemoryCommand(
        scope=SCOPE,
        proposed_content=UserMemoryContent(
            kind="stable_preference",
            key="writing.tone",
            value="concise",
            summary="用户长期偏好简洁表达",
        ),
        origin="system_inference",
        source_refs=SOURCE_REFS,
        confidence=0.85,
        reason="多个已完成任务中重复观察到该偏好",
        expires_at=EXPIRES_AT,
        idempotency_key="candidate-memory-1",
    )


def repository_mock(monkeypatch):
    repository = create_autospec(
        PostgresUserMemoryRepository,
        instance=True,
    )
    monkeypatch.setattr(
        application_module,
        "PostgresUserMemoryRepository",
        lambda session: repository,
    )
    return repository


@pytest.mark.asyncio
async def test_creates_short_term_memory(monkeypatch) -> None:
    repository = repository_mock(monkeypatch)
    repository.get_short_term_by_idempotency_key.return_value = None
    repository.insert_short_term_memory.return_value = True
    memory_id = UUID(int=1)

    outcome = await MemoryWriteApplicationService().create_short_term(
        object(),
        command=short_term_command(),
        now=NOW,
        memory_id=memory_id,
    )

    assert outcome.created is True
    assert outcome.memory.id == memory_id
    assert outcome.memory.created_at == NOW
    repository.insert_short_term_memory.assert_awaited_once_with(
        memory=outcome.memory,
        idempotency_key="short-memory-1",
    )


@pytest.mark.asyncio
async def test_replays_same_short_term_write(monkeypatch) -> None:
    repository = repository_mock(monkeypatch)
    command = short_term_command()
    existing = application_module.ShortTermUserMemory(
        id=UUID(int=2),
        scope=command.scope,
        task_id=command.task_id,
        content=command.content,
        origin=command.origin,
        source_refs=command.source_refs,
        confidence=command.confidence,
        created_at=NOW - timedelta(minutes=1),
        expires_at=command.expires_at,
    )
    repository.get_short_term_by_idempotency_key.return_value = existing

    outcome = await MemoryWriteApplicationService().create_short_term(
        object(),
        command=command,
        now=NOW,
    )

    assert outcome == application_module.ShortTermMemoryWriteOutcome(
        memory=existing,
        created=False,
    )
    repository.insert_short_term_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_reused_short_term_idempotency_key_conflicts(monkeypatch) -> None:
    repository = repository_mock(monkeypatch)
    command = short_term_command()
    existing = application_module.ShortTermUserMemory(
        id=UUID(int=3),
        scope=command.scope,
        task_id=command.task_id,
        content=command.content.model_copy(update={"value": "en-US"}),
        origin=command.origin,
        source_refs=command.source_refs,
        confidence=command.confidence,
        created_at=NOW,
        expires_at=command.expires_at,
    )
    repository.get_short_term_by_idempotency_key.return_value = existing

    with pytest.raises(MemoryWriteConflictError, match="idempotency_key"):
        await MemoryWriteApplicationService().create_short_term(
            object(),
            command=command,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_creates_long_term_candidate_without_promoting(monkeypatch) -> None:
    repository = repository_mock(monkeypatch)
    repository.get_candidate_by_idempotency_key.return_value = None
    repository.insert_memory_candidate.return_value = True
    candidate_id = UUID(int=4)

    outcome = await MemoryWriteApplicationService().propose_long_term(
        object(),
        command=candidate_command(),
        now=NOW,
        candidate_id=candidate_id,
    )

    assert outcome.created is True
    assert outcome.candidate.id == candidate_id
    assert outcome.candidate.status == "pending"
    repository.insert_memory_candidate.assert_awaited_once_with(
        candidate=outcome.candidate,
        idempotency_key="candidate-memory-1",
    )


@pytest.mark.asyncio
async def test_concurrent_candidate_write_replays_winner(monkeypatch) -> None:
    repository = repository_mock(monkeypatch)
    command = candidate_command()
    existing = application_module.LongTermMemoryCandidate(
        id=UUID(int=5),
        scope=command.scope,
        proposed_content=command.proposed_content,
        origin=command.origin,
        source_refs=command.source_refs,
        confidence=command.confidence,
        reason=command.reason,
        created_at=NOW,
        expires_at=command.expires_at,
    )
    repository.get_candidate_by_idempotency_key.side_effect = [None, existing]
    repository.insert_memory_candidate.return_value = False

    outcome = await MemoryWriteApplicationService().propose_long_term(
        object(),
        command=command,
        now=NOW,
    )

    assert outcome.candidate is existing
    assert outcome.created is False


@pytest.mark.asyncio
async def test_database_failure_is_retryable_persistence_error(
    monkeypatch,
) -> None:
    repository = repository_mock(monkeypatch)
    repository.get_short_term_by_idempotency_key.side_effect = (
        SQLAlchemyError("database unavailable")
    )

    with pytest.raises(MemoryWritePersistenceError) as exc_info:
        await MemoryWriteApplicationService().create_short_term(
            object(),
            command=short_term_command(),
            now=NOW,
        )

    assert exc_info.value.retryable is True
