from datetime import datetime, timedelta, timezone

import pytest

from app.analytics.data_source import BehaviorQuery, InMemoryBehaviorDataSource
from app.analytics.entities import BehaviorRecord, ContentType, EventType


START = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def record(
    event_id: str,
    *,
    news_id: str = "news-1",
    event_time: datetime = START,
    content_type: ContentType = ContentType.ARTICLE,
) -> BehaviorRecord:
    return BehaviorRecord(
        event_id=event_id,
        user_id="user-1",
        news_id=news_id,
        event_type=EventType.IMPRESSION,
        event_time=event_time,
        content_type=content_type,
    )


@pytest.mark.parametrize("field", ["event_id", "user_id", "news_id"])
def test_record_rejects_blank_identifiers(field: str) -> None:
    values = {
        "event_id": "event-1",
        "user_id": "user-1",
        "news_id": "news-1",
    }
    values[field] = "  "
    candidate = BehaviorRecord(
        **values,
        event_type=EventType.CLICK,
        event_time=START,
        content_type=ContentType.ARTICLE,
    )

    with pytest.raises(ValueError, match=field):
        candidate.validate()


def test_record_rejects_naive_datetime() -> None:
    candidate = record("event-1", event_time=datetime(2026, 9, 3, 1, 0))
    with pytest.raises(ValueError, match="timezone"):
        candidate.validate()


def test_record_rejects_duration_on_non_consumption_event() -> None:
    candidate = BehaviorRecord(
        event_id="event-1",
        user_id="user-1",
        news_id="news-1",
        event_type=EventType.CLICK,
        event_time=START,
        content_type=ContentType.ARTICLE,
        duration_seconds=3,
    )
    with pytest.raises(ValueError, match="duration"):
        candidate.validate()


def test_query_rejects_invalid_window() -> None:
    with pytest.raises(ValueError, match="less than"):
        BehaviorQuery(start=START, end=START).validate()


@pytest.mark.asyncio
async def test_in_memory_source_filters_with_left_closed_right_open_window() -> None:
    source = InMemoryBehaviorDataSource(
        [
            record("at-start", event_time=START),
            record("inside", event_time=START + timedelta(minutes=30)),
            record("at-end", event_time=END),
            record("other-news", news_id="news-2"),
            record("video", content_type=ContentType.VIDEO),
        ]
    )
    query = BehaviorQuery(
        start=START,
        end=END,
        news_ids=frozenset({"news-1"}),
        content_types=frozenset({ContentType.ARTICLE}),
    )

    assert [item.event_id for item in await source.fetch(query)] == [
        "at-start",
        "inside",
    ]


@pytest.mark.asyncio
async def test_in_memory_source_does_not_mutate_input() -> None:
    records = [record("event-1")]
    source = InMemoryBehaviorDataSource(records)

    result = await source.fetch(BehaviorQuery(start=START, end=END))
    result.clear()

    fetched_again = await source.fetch(BehaviorQuery(start=START, end=END))
    assert [item.event_id for item in fetched_again] == [
        "event-1"
    ]
