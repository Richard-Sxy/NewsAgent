"""历史基线计算练习；实现对应 TODO 后逐个删除 skip。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.analytics.baseline import BaselineCalculator
from app.analytics.entities import ContentType
from app.analytics.metrics import NewsMetricSnapshot


START = datetime(2026, 9, 1, tzinfo=timezone.utc)


def snapshot(
    *,
    news_id: str = "news-1",
    content_type: ContentType = ContentType.ARTICLE,
    day_offset: int = 0,
    impressions: int = 100,
    clicks: int = 20,
    unique_users: int = 30,
    duration: int = 300,
    consumptions: int = 10,
    interactions: int = 2,
) -> NewsMetricSnapshot:
    window_start = START + timedelta(days=day_offset)
    return NewsMetricSnapshot(
        news_id=news_id,
        content_type=content_type,
        window_start=window_start,
        window_end=window_start + timedelta(hours=1),
        impressions=impressions,
        clicks=clicks,
        unique_users=unique_users,
        total_duration_seconds=duration,
        effective_consumptions=consumptions,
        interactions=interactions,
        ctr=Decimal(clicks) / Decimal(impressions) if impressions else Decimal("0"),
    )


def test_baseline_rejects_empty_history() -> None:
    with pytest.raises(ValueError, match="empty|空"):
        BaselineCalculator().calculate([])


def test_baseline_rejects_mixed_news() -> None:
    with pytest.raises(ValueError, match="news_id"):
        BaselineCalculator().calculate([snapshot(), snapshot(news_id="news-2")])


def test_baseline_calculates_decimal_averages() -> None:
    baseline = BaselineCalculator().calculate(
        [
            snapshot(day_offset=0, impressions=100, clicks=10, consumptions=6),
            snapshot(day_offset=1, impressions=200, clicks=40, consumptions=14),
        ]
    )

    assert baseline.sample_count == 2
    assert baseline.impressions == Decimal("150")
    assert baseline.clicks == Decimal("25")
    assert baseline.effective_consumptions == Decimal("10")
    assert baseline.ctr == Decimal("0.15")


def test_baseline_rejects_negative_metrics() -> None:
    with pytest.raises(ValueError, match="negative|负"):
        BaselineCalculator().calculate([snapshot(clicks=-1)])
