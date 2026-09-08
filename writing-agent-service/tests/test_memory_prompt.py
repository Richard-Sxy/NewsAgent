from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.schemas.user_memory import (
    MemorySourceRef,
    ResolvedMemoryContext,
    ResolvedMemoryItem,
    UserMemoryContent,
    UserMemoryScope,
)
from app.services.memory_prompt import MemoryPromptInputBuilder


NOW = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
SCOPE = UserMemoryScope(tenant_id="tenant-1", user_id="user-1")
SOURCE_REFS = (
    MemorySourceRef(
        source_type="user_message",
        source_id="message-1",
        captured_at=NOW,
    ),
)


def resolved_item(
    *,
    memory_id: UUID,
    key: str,
    value,
    tier: str = "long_term",
) -> ResolvedMemoryItem:
    return ResolvedMemoryItem(
        memory_key=key,
        selected_memory_id=memory_id,
        selected_tier=tier,
        selected_scope=SCOPE,
        content=UserMemoryContent(
            kind=(
                "temporary_preference"
                if tier == "short_term"
                else "stable_preference"
            ),
            key=key,
            value=value,
            summary=f"{key} 的已解析偏好",
        ),
        origin=(
            "explicit_user"
            if tier == "short_term"
            else "approved_candidate"
        ),
        source_refs=SOURCE_REFS,
        confidence=0.9,
        version=2,
    )


def resolved_context(*items: ResolvedMemoryItem) -> ResolvedMemoryContext:
    return ResolvedMemoryContext(
        tenant_id="tenant-1",
        user_id="user-1",
        task_id="task-1",
        resolved_at=NOW,
        memories=items,
    )


def test_maps_resolved_memory_to_minimal_prompt_contract() -> None:
    memory_id = UUID(int=1)
    result = MemoryPromptInputBuilder().build(
        resolved_context(
            resolved_item(
                memory_id=memory_id,
                key="output.language",
                value="zh-CN",
                tier="short_term",
            )
        )
    )

    assert result.resolver_policy_version == "memory-prompt-v1"
    assert result.resolved_at == NOW
    assert len(result.items) == 1
    assert result.items[0].memory_id == memory_id
    assert result.items[0].selected_tier == "short_term"
    assert result.items[0].value == "zh-CN"
    serialized = result.model_dump(mode="json")
    assert "source_refs" not in serialized["items"][0]
    assert "overridden_memory_ids" not in serialized["items"][0]


def test_omits_oversized_values_and_items_over_limit() -> None:
    accepted_id = UUID(int=1)
    oversized_id = UUID(int=2)
    overflow_id = UUID(int=3)
    builder = MemoryPromptInputBuilder(
        max_items=1,
        max_value_chars=20,
    )

    result = builder.build(
        resolved_context(
            resolved_item(
                memory_id=accepted_id,
                key="a",
                value="short",
            ),
            resolved_item(
                memory_id=oversized_id,
                key="b",
                value="x" * 30,
            ),
            resolved_item(
                memory_id=overflow_id,
                key="c",
                value="also-short",
            ),
        )
    )

    assert [item.memory_id for item in result.items] == [accepted_id]
    assert result.omitted_memory_ids == (oversized_id, overflow_id)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"policy_version": "   "}, "policy_version"),
        ({"max_items": 0}, "max_items"),
        ({"max_items": 21}, "max_items"),
        ({"max_value_chars": 0}, "max_value_chars"),
    ],
)
def test_rejects_invalid_prompt_budget_configuration(
    arguments,
    message,
) -> None:
    with pytest.raises(ValueError, match=message):
        MemoryPromptInputBuilder(**arguments)
