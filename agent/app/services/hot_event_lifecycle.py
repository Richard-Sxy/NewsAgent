"""维护持续热点事件：跨窗口关联新闻，更新事件状态。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from app.schemas.hot_event import (
    HotEventLifecyclePolicy,
    HotEventObservation,
    HotEventSnapshot,
)
from app.services.hot_news_orchestration import HotNewsRunResult


class HotEventRepository(Protocol):
    """热点事件状态快照的读写端口。"""

    async def list_open(
        self,
        *,
        tenant_id: str,
    ) -> tuple[HotEventSnapshot, ...]: ...

    async def upsert(
        self,
        *,
        snapshots: tuple[HotEventSnapshot, ...],
    ) -> None: ...


class HotEventLifecycleService:
    """把一次完成的热点运行合并进租户事件台账。"""

    def __init__(
        self,
        *,
        repository: HotEventRepository,
        policy: HotEventLifecyclePolicy | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy or HotEventLifecyclePolicy()

    async def apply_completed_run(
        self,
        *,
        tenant_id: str,
        result: HotNewsRunResult,
        now: datetime | None = None,
    ) -> tuple[HotEventSnapshot, ...]:
        observations = tuple(
            self._to_observation(item)
            for item in result.analyzed_news
        )
        existing = await self._repository.list_open(tenant_id=tenant_id)
        resolved_now = now or datetime.now(timezone.utc)
        planned = self._policy.plan(
            tenant_id=tenant_id,
            observations=observations,
            existing_events=existing,
            now=resolved_now,
        )
        await self._repository.upsert(snapshots=planned)
        return planned

    @staticmethod
    def _to_observation(item) -> HotEventObservation:
        analysis_input = item.analysis_input
        return HotEventObservation(
            news_id=item.news_id,
            title=analysis_input.title,
            content_type=analysis_input.content_type,
            window_end=analysis_input.window_end,
            hot_score=analysis_input.hot_score,
            metrics=analysis_input.metrics.model_dump(mode="json"),
            related_news_ids=tuple(
                related.news_id for related in analysis_input.related_news
            ),
        )
