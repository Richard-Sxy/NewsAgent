"""热点指标练习测试模板。

逐个实现 TODO 后删除对应测试上的 skip 标记。测试中的样例均为虚构数据。
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.analytics.entities import BehaviorRecord, ContentType, EventType
from app.analytics.metrics import NewsMetricCalculator


WINDOW_START = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 9, 2, 11, 0, tzinfo=timezone.utc)


def make_record(
    event_id: str,
    event_type: EventType,
    *,
    user_id: str = "user-1",
    news_id: str = "news-1",
    event_time: datetime = WINDOW_START,
    content_type: ContentType = ContentType.ARTICLE,
    duration_seconds: int = 0,
) -> BehaviorRecord:
    return BehaviorRecord(
        event_id=event_id,
        user_id=user_id,
        news_id=news_id,
        event_type=event_type,
        event_time=event_time,
        content_type=content_type,
        duration_seconds=duration_seconds,
    )


def test_record_rejects_negative_duration() -> None:
    import pytest

    record = make_record("event-1", EventType.READ, duration_seconds=-1)
    with pytest.raises(ValueError, match="duration"):
        record.validate()


def test_calculates_article_metrics() -> None:
    records = [
        make_record("event-1", EventType.IMPRESSION, user_id="user-1"),
        make_record("event-2", EventType.IMPRESSION, user_id="user-2"),
        make_record("event-3", EventType.CLICK, user_id="user-1"),
        make_record("event-4", EventType.READ, duration_seconds=30),
        make_record("event-5", EventType.LIKE),
    ]

    snapshots = NewsMetricCalculator().calculate(
        records,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.impressions == 2
    assert snapshot.clicks == 1
    assert snapshot.unique_users == 2
    assert snapshot.total_duration_seconds == 30
    assert snapshot.effective_consumptions == 1
    assert snapshot.interactions == 1
    assert snapshot.ctr == Decimal("0.5")


def test_uses_left_closed_right_open_window() -> None:
    records = [
        make_record("at-start", EventType.CLICK, event_time=WINDOW_START),
        make_record("at-end", EventType.CLICK, event_time=WINDOW_END),
    ]

    snapshot = NewsMetricCalculator().calculate(
        records,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )[0]

    assert snapshot.clicks == 1


def test_deduplicates_events_when_source_does_not_guarantee_uniqueness() -> None:
    record = make_record("same-event", EventType.CLICK)

    snapshot = NewsMetricCalculator().calculate(
        [record, record],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        source_guarantees_unique_event_id=False,
    )[0]

    assert snapshot.clicks == 1


def test_empty_input_returns_empty_snapshots() -> None:
    assert NewsMetricCalculator().calculate(
        [],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    ) == []
