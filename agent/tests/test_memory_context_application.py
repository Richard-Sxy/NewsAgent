from datetime import datetime, timedelta, timezone
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

import app.services.memory_context_application as application_module
from app.repositories.user_memory import PostgresUserMemoryRepository
from app.schemas.user_memory import (
    LongTermUserMemory,
    MemorySourceRef,
    ResolvedMemoryContext,
    ShortTermUserMemory,
    UserMemoryContent,
    UserMemoryScope,
)
from app.services.memory_context import UserMemoryContextResolver
from app.services.memory_context_application import (
    MemoryContextApplicationService,
    MemoryContextPersistenceError,
)


NOW = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)


def dependencies(monkeypatch):
    repository = create_autospec(
        PostgresUserMemoryRepository,
        instance=True,
    )
    resolver = create_autospec(
        UserMemoryContextResolver,
        instance=True,
    )
    monkeypatch.setattr(
        application_module,
        "PostgresUserMemoryRepository",
        lambda session: repository,
    )
    return repository, resolver


@pytest.mark.asyncio
async def test_reads_both_tiers_and_delegates_deterministic_resolution(
    monkeypatch,
):
    repository, resolver = dependencies(monkeypatch)
    short_term = [object()]
    long_term = [object()]
    expected = ResolvedMemoryContext(
        tenant_id="tenant-1",
        user_id="user-1",
        task_id="task-1",
        team_id="team-news",
        section_id="finance",
        role_id="reviewer",
        resolved_at=NOW,
        memories=(),
    )
    repository.list_active_short_term.return_value = short_term
    repository.list_active_long_term.return_value = long_term
    resolver.resolve.return_value = expected
    service = MemoryContextApplicationService(resolver)

    result = await service.resolve_for_task(
        object(),
        tenant_id=" tenant-1 ",
        user_id=" user-1 ",
        task_id=" task-1 ",
        team_id=" team-news ",
        section_id=" finance ",
        role_id=" reviewer ",
        now=NOW,
    )

    assert result is expected
    repository.list_active_short_term.assert_awaited_once_with(
        tenant_id="tenant-1",
        user_id="user-1",
        task_id="task-1",
        now=NOW,
    )
    repository.list_active_long_term.assert_awaited_once_with(
        tenant_id="tenant-1",
        user_id="user-1",
        now=NOW,
    )
    resolver.resolve.assert_called_once_with(
        tenant_id="tenant-1",
        user_id="user-1",
        task_id="task-1",
        short_term=short_term,
        long_term=long_term,
        now=NOW,
        team_id="team-news",
        section_id="finance",
        role_id="reviewer",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tenant_id", "   ", "tenant_id cannot be empty"),
        ("user_id", "   ", "user_id cannot be empty"),
        ("task_id", "   ", "task_id cannot be empty"),
        ("team_id", "   ", "team_id cannot be blank"),
    ],
)
async def test_rejects_invalid_identity_before_database_query(
    monkeypatch,
    field,
    value,
    message,
):
    repository, resolver = dependencies(monkeypatch)
    arguments = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "task_id": "task-1",
        "team_id": None,
        "now": NOW,
    }
    arguments[field] = value
    service = MemoryContextApplicationService(resolver)

    with pytest.raises(ValueError, match=message):
        await service.resolve_for_task(object(), **arguments)

    repository.list_active_short_term.assert_not_awaited()
    repository.list_active_long_term.assert_not_awaited()
    resolver.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_naive_time_before_database_query(monkeypatch):
    repository, resolver = dependencies(monkeypatch)
    service = MemoryContextApplicationService(resolver)

    with pytest.raises(ValueError, match="timezone-aware"):
        await service.resolve_for_task(
            object(),
            tenant_id="tenant-1",
            user_id="user-1",
            task_id="task-1",
            now=datetime(2026, 9, 8, 10),
        )

    repository.list_active_short_term.assert_not_awaited()
    repository.list_active_long_term.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_method",
    ["list_active_short_term", "list_active_long_term"],
)
async def test_maps_database_failure_to_retryable_domain_error(
    monkeypatch,
    failing_method,
):
    repository, resolver = dependencies(monkeypatch)
    repository.list_active_short_term.return_value = []
    repository.list_active_long_term.return_value = []
    getattr(repository, failing_method).side_effect = SQLAlchemyError(
        "database unavailable"
    )
    service = MemoryContextApplicationService(resolver)

    with pytest.raises(MemoryContextPersistenceError, match="读取") as exc:
        await service.resolve_for_task(
            object(),
            tenant_id="tenant-1",
            user_id="user-1",
            task_id="task-1",
            now=NOW,
        )

    assert exc.value.retryable is True
    resolver.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_application_service_uses_real_resolver_to_merge_both_tiers(
    monkeypatch,
):
    repository = create_autospec(
        PostgresUserMemoryRepository,
        instance=True,
    )
    monkeypatch.setattr(
        application_module,
        "PostgresUserMemoryRepository",
        lambda session: repository,
    )
    scope = UserMemoryScope(
        tenant_id="tenant-1",
        user_id="user-1",
    )
    source_refs = (
        MemorySourceRef(
            source_type="user_message",
            source_id="message-1",
            captured_at=NOW,
        ),
    )
    long_term = LongTermUserMemory(
        id=uuid4(),
        scope=scope,
        content=UserMemoryContent(
            kind="stable_preference",
            key="output.language",
            value="en",
            summary="长期默认使用英文",
        ),
        origin="explicit_user",
        source_refs=source_refs,
        confidence=1,
        confirmed_by="user-1",
        confirmed_at=NOW - timedelta(days=1),
        valid_from=NOW - timedelta(days=1),
        recorded_at=NOW - timedelta(days=1),
    )
    short_term = ShortTermUserMemory(
        id=uuid4(),
        scope=scope,
        task_id="task-1",
        content=UserMemoryContent(
            kind="temporary_preference",
            key="output.language",
            value="zh-CN",
            summary="本次任务使用中文",
        ),
        origin="explicit_user",
        source_refs=source_refs,
        confidence=1,
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    repository.list_active_short_term.return_value = [short_term]
    repository.list_active_long_term.return_value = [long_term]

    result = await MemoryContextApplicationService().resolve_for_task(
        object(),
        tenant_id="tenant-1",
        user_id="user-1",
        task_id="task-1",
        now=NOW,
    )

    assert len(result.memories) == 1
    selected = result.memories[0]
    assert selected.selected_memory_id == short_term.id
    assert selected.selected_tier == "short_term"
    assert selected.content.value == "zh-CN"
    assert selected.overridden_memory_ids == (long_term.id,)
