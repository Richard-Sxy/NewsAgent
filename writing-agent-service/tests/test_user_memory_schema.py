from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.user_memory import (
    LongTermMemoryCandidate,
    LongTermUserMemory,
    MemorySourceRef,
    ShortTermUserMemory,
    UserMemoryContent,
    UserMemoryScope,
)


NOW = datetime(2026, 9, 7, 10, tzinfo=timezone.utc)


def scope() -> UserMemoryScope:
    return UserMemoryScope(tenant_id="tenant-1", user_id="user-1")


def content() -> UserMemoryContent:
    return UserMemoryContent(
        kind="stable_preference",
        key="output.language",
        value="zh-CN",
        summary="使用中文",
    )


def sources() -> tuple[MemorySourceRef, ...]:
    return (
        MemorySourceRef(
            source_type="user_message",
            source_id="message-1",
            captured_at=NOW,
        ),
    )


def test_scope_strips_values_and_rejects_blank_identity() -> None:
    value = UserMemoryScope(
        tenant_id=" tenant-1 ",
        user_id=" user-1 ",
    )
    assert value.tenant_id == "tenant-1"
    assert value.user_id == "user-1"

    with pytest.raises(ValidationError):
        UserMemoryScope(tenant_id="   ", user_id="user-1")


def test_source_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValidationError):
        MemorySourceRef(
            source_type="user_message",
            source_id="message-1",
            captured_at=datetime(2026, 9, 7, 10),
        )


def test_short_term_requires_source_and_valid_time_window() -> None:
    values = {
        "id": uuid4(),
        "scope": scope(),
        "task_id": "task-1",
        "content": content(),
        "origin": "explicit_user",
        "source_refs": sources(),
        "confidence": 1,
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    ShortTermUserMemory(**values)

    with pytest.raises(ValidationError, match="at least 1"):
        ShortTermUserMemory(**(values | {"source_refs": ()}))
    with pytest.raises(ValidationError, match="expires_at"):
        ShortTermUserMemory(
            **(values | {"expires_at": NOW - timedelta(seconds=1)})
        )


def test_candidate_rejects_invalid_time_window() -> None:
    with pytest.raises(ValidationError, match="expires_at"):
        LongTermMemoryCandidate(
            id=uuid4(),
            scope=scope(),
            proposed_content=content(),
            origin="system_inference",
            source_refs=sources(),
            confidence=0.8,
            reason="多次观察到相同偏好",
            created_at=NOW,
            expires_at=NOW,
        )


def test_long_term_rejects_invalid_validity_and_confirmation_order() -> None:
    values = {
        "id": uuid4(),
        "scope": scope(),
        "content": content(),
        "origin": "approved_candidate",
        "source_refs": sources(),
        "confidence": 0.9,
        "confirmed_by": "user-1",
        "confirmed_at": NOW,
        "valid_from": NOW,
        "recorded_at": NOW,
    }
    LongTermUserMemory(**values)

    with pytest.raises(ValidationError, match="valid_until"):
        LongTermUserMemory(**(values | {"valid_until": NOW}))
    with pytest.raises(ValidationError, match="confirmed_at"):
        LongTermUserMemory(
            **(
                values
                | {
                    "confirmed_at": NOW + timedelta(seconds=1),
                    "recorded_at": NOW,
                }
            )
        )
