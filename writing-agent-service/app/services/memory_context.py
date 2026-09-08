"""用户长短期记忆的作用域过滤与确定性冲突解析。"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.schemas.user_memory import (
    LongTermUserMemory,
    ResolvedMemoryContext,
    ResolvedMemoryItem,
    ShortTermUserMemory,
)


MemoryRecord = ShortTermUserMemory | LongTermUserMemory
MemoryTier = Literal["short_term", "long_term"]


@dataclass(frozen=True, slots=True)
class _RankedMemory:
    tier: MemoryTier
    record: MemoryRecord
    priority: int
    effective_at: datetime


class UserMemoryContextResolver:
    """只解析个人记忆；租户合规和安全硬规则由更高层合并。"""

    _PRIORITIES: dict[tuple[MemoryTier, str], int] = {
        ("short_term", "explicit_user"): 5,
        ("long_term", "explicit_user"): 4,
        ("long_term", "approved_candidate"): 3,
        ("long_term", "trusted_identity"): 2,
        ("short_term", "system_inference"): 1,
    }

    def resolve(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
        short_term: list[ShortTermUserMemory],
        long_term: list[LongTermUserMemory],
        now: datetime,
        team_id: str | None = None,
        section_id: str | None = None,
        role_id: str | None = None,
    ) -> ResolvedMemoryContext:
        tenant_id = self._normalize_required("tenant_id", tenant_id)
        user_id = self._normalize_required("user_id", user_id)
        task_id = self._normalize_required("task_id", task_id)
        team_id = self._normalize_optional("team_id", team_id)
        section_id = self._normalize_optional("section_id", section_id)
        role_id = self._normalize_optional("role_id", role_id)
        if not self._is_timezone_aware(now):
            raise ValueError("now must be timezone-aware")

        grouped: dict[str, list[_RankedMemory]] = defaultdict(list)
        for memory in short_term:
            if not self._matches_scope(
                memory=memory,
                tenant_id=tenant_id,
                user_id=user_id,
                team_id=team_id,
                section_id=section_id,
                role_id=role_id,
            ):
                continue
            if memory.task_id != task_id or memory.status != "active":
                continue
            if not (memory.created_at <= now < memory.expires_at):
                continue

            priority = self._PRIORITIES[("short_term", memory.origin)]
            grouped[memory.content.key].append(
                _RankedMemory(
                    tier="short_term",
                    record=memory,
                    priority=priority,
                    effective_at=memory.created_at,
                )
            )

        for memory in long_term:
            if not self._matches_scope(
                memory=memory,
                tenant_id=tenant_id,
                user_id=user_id,
                team_id=team_id,
                section_id=section_id,
                role_id=role_id,
            ):
                continue
            if memory.status != "active" or memory.valid_from > now:
                continue
            if memory.valid_until is not None and now >= memory.valid_until:
                continue

            priority = self._PRIORITIES[("long_term", memory.origin)]
            grouped[memory.content.key].append(
                _RankedMemory(
                    tier="long_term",
                    record=memory,
                    priority=priority,
                    effective_at=memory.valid_from,
                )
            )

        resolved_items: list[ResolvedMemoryItem] = []
        for memory_key in sorted(grouped):
            ranked = sorted(grouped[memory_key], key=self._sort_key)
            winner = ranked[0]
            resolved_items.append(
                ResolvedMemoryItem(
                    memory_key=memory_key,
                    selected_memory_id=winner.record.id,
                    selected_tier=winner.tier,
                    selected_scope=winner.record.scope,
                    content=winner.record.content.model_copy(deep=True),
                    origin=winner.record.origin,
                    source_refs=winner.record.source_refs,
                    confidence=winner.record.confidence,
                    version=winner.record.version,
                    overridden_memory_ids=tuple(
                        item.record.id for item in ranked[1:]
                    ),
                )
            )

        return ResolvedMemoryContext(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
            team_id=team_id,
            section_id=section_id,
            role_id=role_id,
            resolved_at=now,
            memories=tuple(resolved_items),
        )

    @classmethod
    def _matches_scope(
        cls,
        *,
        memory: MemoryRecord,
        tenant_id: str,
        user_id: str,
        team_id: str | None,
        section_id: str | None,
        role_id: str | None,
    ) -> bool:
        return (
            memory.scope.tenant_id == tenant_id
            and memory.scope.user_id == user_id
            and cls._optional_scope_matches(
                stored=memory.scope.team_id,
                current=team_id,
            )
            and cls._optional_scope_matches(
                stored=memory.scope.section_id,
                current=section_id,
            )
            and cls._optional_scope_matches(
                stored=memory.scope.role_id,
                current=role_id,
            )
        )

    @staticmethod
    def _optional_scope_matches(
        *,
        stored: str | None,
        current: str | None,
    ) -> bool:
        return stored is None or stored == current

    @staticmethod
    def _scope_specificity(memory: MemoryRecord) -> int:
        return sum(
            value is not None
            for value in (
                memory.scope.team_id,
                memory.scope.section_id,
                memory.scope.role_id,
            )
        )

    @classmethod
    def _sort_key(
        cls,
        item: _RankedMemory,
    ) -> tuple[int, int, int, float, str]:
        return (
            -item.priority,
            -cls._scope_specificity(item.record),
            -item.record.version,
            -item.effective_at.timestamp(),
            str(item.record.id),
        )

    @staticmethod
    def _is_timezone_aware(value: datetime) -> bool:
        return value.tzinfo is not None and value.utcoffset() is not None

    @staticmethod
    def _normalize_required(name: str, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{name} cannot be empty")
        return normalized

    @staticmethod
    def _normalize_optional(name: str, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{name} cannot be blank when provided")
        return normalized
