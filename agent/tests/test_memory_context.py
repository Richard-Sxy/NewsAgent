from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.schemas.user_memory import (
    LongTermUserMemory,
    MemorySourceRef,
    ShortTermUserMemory,
    UserMemoryContent,
    UserMemoryScope,
)
from app.services.memory_context import UserMemoryContextResolver


NOW = datetime(2026, 9, 7, 10, tzinfo=timezone.utc)


def source() -> tuple[MemorySourceRef, ...]:
    return (
        MemorySourceRef(
            source_type="user_message",
            source_id="message-1",
            captured_at=NOW,
        ),
    )


def content(value: str) -> UserMemoryContent:
    return UserMemoryContent(
        kind="stable_preference",
        key="output.language",
        value=value,
        summary=f"使用 {value}",
    )


def short_memory(
    value: str,
    *,
    scope: UserMemoryScope | None = None,
    task_id: str = "task-1",
) -> ShortTermUserMemory:
    return ShortTermUserMemory(
        id=uuid4(),
        scope=scope or UserMemoryScope(
            tenant_id="tenant-1",
            user_id="user-1",
        ),
        task_id=task_id,
        content=content(value),
        origin="explicit_user",
        source_refs=source(),
        confidence=1,
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )


def long_memory(
    value: str,
    *,
    scope: UserMemoryScope | None = None,
    memory_id: UUID | None = None,
) -> LongTermUserMemory:
    return LongTermUserMemory(
        id=memory_id or uuid4(),
        scope=scope or UserMemoryScope(
            tenant_id="tenant-1",
            user_id="user-1",
        ),
        content=content(value),
        origin="explicit_user",
        source_refs=source(),
        confidence=1,
        confirmed_by="user-1",
        confirmed_at=NOW - timedelta(days=1),
        valid_from=NOW - timedelta(days=1),
        recorded_at=NOW - timedelta(days=1),
    )


def resolve(short_term=(), long_term=(), **scope_values):
    return UserMemoryContextResolver().resolve(
        tenant_id="tenant-1",
        user_id="user-1",
        task_id="task-1",
        short_term=list(short_term),
        long_term=list(long_term),
        now=NOW,
        **scope_values,
    )


def test_explicit_short_term_overrides_long_term_for_current_task() -> None:
    short = short_memory("zh-CN")
    long = long_memory("en")

    result = resolve([short], [long])

    assert result.memories[0].selected_memory_id == short.id
    assert result.memories[0].selected_tier == "short_term"
    assert result.memories[0].overridden_memory_ids == (long.id,)


def test_other_task_short_term_memory_is_ignored() -> None:
    short = short_memory("zh-CN", task_id="task-other")
    long = long_memory("en")

    result = resolve([short], [long])

    assert result.memories[0].selected_memory_id == long.id


def test_scoped_memory_requires_matching_trusted_context() -> None:
    scoped = long_memory(
        "zh-CN",
        scope=UserMemoryScope(
            tenant_id="tenant-1",
            user_id="user-1",
            team_id="team-news",
            section_id="finance",
            role_id="reviewer",
        ),
    )

    assert resolve(long_term=[scoped]).memories == ()
    assert resolve(
        long_term=[scoped],
        team_id="team-other",
        section_id="finance",
        role_id="reviewer",
    ).memories == ()
    matched = resolve(
        long_term=[scoped],
        team_id="team-news",
        section_id="finance",
        role_id="reviewer",
    )
    assert matched.memories[0].selected_memory_id == scoped.id


def test_other_tenant_or_user_memory_is_ignored() -> None:
    other_tenant = long_memory(
        "zh-CN",
        scope=UserMemoryScope(
            tenant_id="tenant-other",
            user_id="user-1",
        ),
    )
    other_user = long_memory(
        "en",
        scope=UserMemoryScope(
            tenant_id="tenant-1",
            user_id="user-other",
        ),
    )

    assert resolve(long_term=[other_tenant, other_user]).memories == ()


def test_global_memory_applies_inside_specific_context() -> None:
    global_memory = long_memory("zh-CN")

    result = resolve(
        long_term=[global_memory],
        team_id="team-news",
        section_id="finance",
        role_id="reviewer",
    )

    assert result.memories[0].selected_memory_id == global_memory.id


def test_resolution_is_stable_when_input_order_changes() -> None:
    older = long_memory("en", memory_id=UUID(int=2))
    newer = long_memory("zh-CN", memory_id=UUID(int=1)).model_copy(
        update={"version": 2}
    )

    first = resolve(long_term=[older, newer])
    second = resolve(long_term=[newer, older])

    assert first == second
    assert first.memories[0].selected_memory_id == newer.id


def test_expired_and_revoked_memories_are_ignored() -> None:
    expired = short_memory("zh-CN").model_copy(
        update={"expires_at": NOW}
    )
    revoked = long_memory("en").model_copy(update={"status": "revoked"})

    assert resolve([expired], [revoked]).memories == ()
