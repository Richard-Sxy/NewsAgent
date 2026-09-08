from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.schemas.user_memory import (
    LongTermMemoryCandidate,
    LongTermUserMemory,
    MemorySourceRef,
    PromoteMemoryCandidateCommand,
    UserMemoryContent,
    UserMemoryScope,
)
from app.services.memory_promotion import (
    MemoryPromotionRejectedError,
    MemoryPromotionService,
)


NOW = datetime(2026, 9, 7, 10, tzinfo=timezone.utc)


def candidate(**updates) -> LongTermMemoryCandidate:
    values = {
        "id": uuid4(),
        "scope": UserMemoryScope(
            tenant_id="tenant-1",
            user_id="user-1",
        ),
        "proposed_content": UserMemoryContent(
            kind="stable_preference",
            key="output.language",
            value="zh-CN",
            summary="使用中文",
        ),
        "origin": "system_inference",
        "source_refs": (
            MemorySourceRef(
                source_type="system_observation",
                source_id="observation-1",
                captured_at=NOW - timedelta(hours=1),
            ),
        ),
        "confidence": 0.8,
        "reason": "多次观察到相同偏好",
        "created_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(days=7),
    }
    values.update(updates)
    return LongTermMemoryCandidate(**values)


def command(
    value: LongTermMemoryCandidate,
    **updates,
) -> PromoteMemoryCandidateCommand:
    values = {
        "tenant_id": value.scope.tenant_id,
        "user_id": value.scope.user_id,
        "candidate_id": value.id,
        "approved_by": "user-1",
        "expected_version": value.version,
        "idempotency_key": "promotion-request-1",
    }
    values.update(updates)
    return PromoteMemoryCandidateCommand(**values)


def test_approved_system_inference_becomes_long_term_memory() -> None:
    value = candidate()
    memory_id = uuid4()

    result = MemoryPromotionService().promote(
        candidate=value,
        command=command(value),
        now=NOW,
        memory_id=memory_id,
    )

    assert result.approved_candidate.status == "approved"
    assert result.approved_candidate.version == value.version + 1
    assert result.long_term_memory.id == memory_id
    assert result.long_term_memory.origin == "approved_candidate"
    assert result.long_term_memory.confirmed_by == "user-1"
    assert result.idempotency_key == "promotion-request-1"


@pytest.mark.parametrize(
    "command_update",
    [
        {"tenant_id": "tenant-other"},
        {"user_id": "user-other"},
        {"candidate_id": uuid4()},
    ],
)
def test_scope_or_target_mismatch_is_rejected_uniformly(command_update) -> None:
    value = candidate()

    with pytest.raises(
        MemoryPromotionRejectedError,
        match="not available",
    ):
        MemoryPromotionService().promote(
            candidate=value,
            command=command(value, **command_update),
            now=NOW,
        )


def test_future_or_expired_candidate_is_rejected() -> None:
    future = candidate(
        created_at=NOW + timedelta(days=1),
        expires_at=NOW + timedelta(days=2),
    )
    expired = candidate(
        created_at=NOW - timedelta(days=2),
        expires_at=NOW,
    )

    for value in (future, expired):
        with pytest.raises(
            MemoryPromotionRejectedError,
            match="not currently active",
        ):
            MemoryPromotionService().promote(
                candidate=value,
                command=command(value),
                now=NOW,
            )


def test_non_promotable_kind_and_version_conflict_are_rejected() -> None:
    task_candidate = candidate(
        proposed_content=UserMemoryContent(
            kind="task_goal",
            key="task.goal",
            value="finish",
            summary="完成当前任务",
        )
    )
    with pytest.raises(MemoryPromotionRejectedError, match="not eligible"):
        MemoryPromotionService().promote(
            candidate=task_candidate,
            command=command(task_candidate),
            now=NOW,
        )

    value = candidate()
    with pytest.raises(MemoryPromotionRejectedError, match="version conflict"):
        MemoryPromotionService().promote(
            candidate=value,
            command=command(value, expected_version=value.version + 1),
            now=NOW,
        )


def test_superseded_memory_must_match_scope_key_and_target() -> None:
    value = candidate()
    old = LongTermUserMemory(
        id=uuid4(),
        scope=value.scope,
        content=value.proposed_content.model_copy(update={"value": "en"}),
        origin="explicit_user",
        source_refs=value.source_refs,
        confidence=1,
        confirmed_by="user-1",
        confirmed_at=NOW - timedelta(days=2),
        valid_from=NOW - timedelta(days=2),
        recorded_at=NOW - timedelta(days=2),
        version=3,
    )
    promotion_command = command(value, supersedes_memory_id=old.id)

    result = MemoryPromotionService().promote(
        candidate=value,
        command=promotion_command,
        now=NOW,
        superseded_memory=old,
    )
    assert result.long_term_memory.supersedes_memory_id == old.id
    assert result.long_term_memory.version == 4

    with pytest.raises(MemoryPromotionRejectedError, match="not available"):
        MemoryPromotionService().promote(
            candidate=value,
            command=promotion_command,
            now=NOW,
            superseded_memory=old.model_copy(
                update={
                    "scope": old.scope.model_copy(
                        update={"tenant_id": "tenant-other"}
                    )
                }
            ),
        )
