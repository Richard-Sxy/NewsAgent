from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.analytics.entities import ContentType
from app.analytics.hot_score import HotScoreCalculator, HotScoreConfig
from app.analytics.metrics import NewsMetricSnapshot


START = datetime(2026, 9, 3, tzinfo=timezone.utc)


def snapshot(
    *,
    news_id: str = "news-1",
    content_type: ContentType = ContentType.ARTICLE,
    impressions: int = 100,
    clicks: int = 20,
    effective_consumptions: int = 10,
    interactions: int = 2,
) -> NewsMetricSnapshot:
    return NewsMetricSnapshot(
        news_id=news_id,
        content_type=content_type,
        window_start=START,
        window_end=START + timedelta(hours=1),
        impressions=impressions,
        clicks=clicks,
        unique_users=20,
        total_duration_seconds=300,
        effective_consumptions=effective_consumptions,
        interactions=interactions,
        ctr=Decimal(clicks) / Decimal(impressions) if impressions else Decimal("0"),
    )


def test_default_hot_score_weights_are_valid() -> None:
    HotScoreConfig().validate()


@pytest.mark.parametrize(
    "config",
    [
        HotScoreConfig(click_weight=Decimal("-0.1")),
        HotScoreConfig(click_weight=Decimal("1.1")),
        HotScoreConfig(click_weight=Decimal("0.31")),
    ],
)
def test_invalid_hot_score_weights_are_rejected(config: HotScoreConfig) -> None:
    with pytest.raises(ValueError):
        config.validate()


def test_hot_score_without_baseline_has_no_growth_component() -> None:
    result = HotScoreCalculator().calculate(snapshot(), None)

    assert result.growth_component == Decimal("0")
    assert result.score == (
        result.click_component
        + result.consumption_component
        + result.interaction_component
        + result.growth_component
    )


def test_hot_score_rewards_positive_growth() -> None:
    result = HotScoreCalculator().calculate(
        snapshot(clicks=40, effective_consumptions=20, interactions=4),
        snapshot(clicks=20, effective_consumptions=10, interactions=2),
    )

    assert result.growth_component > 0
    assert Decimal("0") <= result.score <= Decimal("1")


def test_hot_score_does_not_reward_decline() -> None:
    result = HotScoreCalculator().calculate(
        snapshot(clicks=10, effective_consumptions=5, interactions=1),
        snapshot(clicks=20, effective_consumptions=10, interactions=2),
    )

    assert result.growth_component == Decimal("0")


def test_hot_score_rejects_mismatched_baseline() -> None:
    with pytest.raises(ValueError, match="news_id"):
        HotScoreCalculator().calculate(snapshot(), snapshot(news_id="news-2"))
