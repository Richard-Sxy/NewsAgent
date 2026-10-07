"""热点排行榜练习；实现对应 TODO 后逐个删除 skip。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.analytics.baseline import NewsMetricBaseline
from app.analytics.entities import ContentType
from app.analytics.metrics import NewsMetricSnapshot
from app.analytics.ranking import HotNewsRanker


START = datetime(2026, 9, 3, tzinfo=timezone.utc)


def snapshot(news_id: str, *, clicks: int, consumptions: int) -> NewsMetricSnapshot:
    return NewsMetricSnapshot(
        news_id=news_id,
        content_type=ContentType.ARTICLE,
        window_start=START,
        window_end=START + timedelta(hours=1),
        impressions=100,
        clicks=clicks,
        unique_users=clicks,
        total_duration_seconds=consumptions * 20,
        effective_consumptions=consumptions,
        interactions=0,
        ctr=Decimal(clicks) / Decimal("100"),
    )


def baseline(news_id: str) -> NewsMetricBaseline:
    return NewsMetricBaseline(
        news_id=news_id,
        content_type=ContentType.ARTICLE,
        sample_count=7,
        impressions=Decimal("100"),
        clicks=Decimal("10"),
        unique_users=Decimal("10"),
        total_duration_seconds=Decimal("100"),
        effective_consumptions=Decimal("5"),
        interactions=Decimal("0"),
        ctr=Decimal("0.1"),
    )


def test_ranking_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        HotNewsRanker().rank([], {}, limit=0)


def test_ranking_returns_empty_list_for_empty_input() -> None:
    assert HotNewsRanker().rank([], {}) == []


def test_ranking_rejects_duplicate_metric_key() -> None:
    item = snapshot("news-1", clicks=20, consumptions=10)
    with pytest.raises(ValueError, match="duplicate|重复"):
        HotNewsRanker().rank([item, item], {})


def test_ranking_orders_by_score_and_applies_limit() -> None:
    current = [
        snapshot("news-low", clicks=10, consumptions=5),
        snapshot("news-high", clicks=50, consumptions=30),
        snapshot("news-mid", clicks=30, consumptions=15),
    ]
    baselines = {
        (item.news_id, item.content_type): baseline(item.news_id) for item in current
    }

    ranked = HotNewsRanker().rank(current, baselines, limit=2)

    assert [item.current.news_id for item in ranked] == ["news-high", "news-mid"]
    assert [item.rank for item in ranked] == [1, 2]


def test_ranking_allows_missing_baseline() -> None:
    ranked = HotNewsRanker().rank(
        [snapshot("news-new", clicks=30, consumptions=20)],
        {},
    )

    assert len(ranked) == 1
    assert ranked[0].baseline is None
    assert ranked[0].hot_score.growth_component == Decimal("0")
