"""热点事件去重、合并与生命周期状态机测试。

只验证确定性领域规则和服务编排，不访问数据库。
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.hot_event import (
    HotEventLifecyclePolicy,
    HotEventObservation,
)
from app.schemas.hot_news import (
    HotNewsAnalysisInput,
    HotNewsMetrics,
    HotScoreComponents,
)
from app.services.hot_event_lifecycle import HotEventLifecycleService


NOW = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
TENANT_ID = "tenant-1"


def observation(
    news_id: str,
    *,
    title: str = "热点事件",
    window_end: datetime = NOW,
    hot_score: float = 0.8,
    related: tuple[str, ...] = (),
) -> HotEventObservation:
    return HotEventObservation(
        news_id=news_id,
        title=title,
        content_type="article",
        window_end=window_end,
        hot_score=hot_score,
        metrics={"impressions": 100, "clicks": 10},
        related_news_ids=related,
    )


def make_input(news_id: str) -> HotNewsAnalysisInput:
    return HotNewsAnalysisInput(
        news_id=news_id,
        title=f"标题 {news_id}",
        content_type="article",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        hot_score=0.8,
        metrics=HotNewsMetrics(
            impressions=100,
            clicks=10,
            ctr=0.1,
            unique_users=9,
            effective_consumptions=8,
            interactions=2,
        ),
        score_components=HotScoreComponents(
            click=0.3,
            consumption=0.3,
            interaction=0.1,
            growth=0.1,
        ),
        analysis_policy_version="hot-news-analysis-v1",
    )


def test_first_observation_creates_emerging_event() -> None:
    policy = HotEventLifecyclePolicy()

    planned = policy.plan(
        tenant_id=TENANT_ID,
        observations=(observation("n1"),),
        existing_events=(),
        now=NOW,
    )

    assert len(planned) == 1
    event = planned[0]
    assert event.state == "emerging"
    assert event.occurrence_count == 1
    assert event.member_news_ids == ("n1",)
    assert event.version == 1


def test_repeated_window_advances_to_active() -> None:
    policy = HotEventLifecyclePolicy()
    first = policy.plan(
        tenant_id=TENANT_ID,
        observations=(observation("n1"),),
        existing_events=(),
        now=NOW,
    )[0]

    second = policy.plan(
        tenant_id=TENANT_ID,
        observations=(observation("n1", window_end=NOW + timedelta(hours=1)),),
        existing_events=(first,),
        now=NOW + timedelta(hours=1),
    )[0]

    assert second.event_key == first.event_key
    assert second.state == "active"
    assert second.occurrence_count == 2
    assert second.version == 2


def test_same_window_replay_is_idempotent() -> None:
    policy = HotEventLifecyclePolicy()
    first = policy.plan(
        tenant_id=TENANT_ID,
        observations=(observation("n1"),),
        existing_events=(),
        now=NOW,
    )[0]

    replay = policy.plan(
        tenant_id=TENANT_ID,
        observations=(observation("n1"),),
        existing_events=(first,),
        now=NOW,
    )[0]

    assert replay.occurrence_count == 1
    assert replay.state == "emerging"
    assert replay.version == 1


def test_related_edge_merges_into_existing_event() -> None:
    policy = HotEventLifecyclePolicy()
    first = policy.plan(
        tenant_id=TENANT_ID,
        observations=(observation("n1"),),
        existing_events=(),
        now=NOW,
    )[0]

    merged = policy.plan(
        tenant_id=TENANT_ID,
        observations=(
            observation(
                "n2",
                window_end=NOW + timedelta(hours=1),
                related=("n1",),
            ),
        ),
        existing_events=(first,),
        now=NOW + timedelta(hours=1),
    )

    assert len(merged) == 1
    assert merged[0].event_key == first.event_key
    assert set(merged[0].member_news_ids) == {"n1", "n2"}
    assert merged[0].distinct_news_count == 2


def test_unobserved_event_ages_to_cooling_then_closed() -> None:
    policy = HotEventLifecyclePolicy()
    base = policy.plan(
        tenant_id=TENANT_ID,
        observations=(observation("n1"),),
        existing_events=(),
        now=NOW,
    )[0]

    cooling = policy.plan(
        tenant_id=TENANT_ID,
        observations=(),
        existing_events=(base,),
        now=NOW + timedelta(hours=7),
    )[0]
    assert cooling.state == "cooling"

    closed = policy.plan(
        tenant_id=TENANT_ID,
        observations=(),
        existing_events=(cooling,),
        now=NOW + timedelta(hours=25),
    )[0]
    assert closed.state == "closed"


@pytest.mark.asyncio
async def test_service_applies_completed_run() -> None:
    repository = SimpleNamespace(
        list_open=AsyncMock(return_value=()),
        upsert=AsyncMock(),
    )
    service = HotEventLifecycleService(repository=repository)
    result = SimpleNamespace(
        analyzed_news=[
            SimpleNamespace(news_id="n1", analysis_input=make_input("n1")),
        ]
    )

    planned = await service.apply_completed_run(
        tenant_id=TENANT_ID,
        result=result,
        now=NOW,
    )

    assert len(planned) == 1
    assert planned[0].member_news_ids == ("n1",)
    repository.list_open.assert_awaited_once_with(tenant_id=TENANT_ID)
    repository.upsert.assert_awaited_once_with(snapshots=planned)
