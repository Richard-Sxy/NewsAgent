import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from redis.exceptions import ConnectionError

from app.domain.execution import StepType
from app.domain.job_status import JobStatus
from app.schemas.events import ProgressEvent
from app.services.event_publisher import ProgressPublishError, RedisProgressPublisher


class FakeRedis:
    def __init__(self, result=b"1720000000000-0") -> None:
        self.result = result
        self.calls = []
        self.error = None

    async def eval(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result


def settings():
    return SimpleNamespace(
        progress_stream_prefix="news-writing:events",
        progress_stream_maxlen=2000,
        progress_dedup_ttl_seconds=86400,
    )


def event() -> ProgressEvent:
    return ProgressEvent(
        event="step.completed",
        deduplication_key="job-1:research:attempt-1:completed",
        tenant_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        step=StepType.RESEARCH,
        status=JobStatus.RESEARCH_REVIEW,
        progress={"completed": 1, "total": 1, "percent": 15},
        occurred_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_publish_returns_redis_stream_id() -> None:
    redis = FakeRedis()
    publisher = RedisProgressPublisher(redis, settings())
    progress_event = event()

    published = await publisher.publish(progress_event)

    assert published.event_id == "1720000000000-0"
    call = redis.calls[0]
    assert "XADD" in call[0]
    assert call[1] == 2
    assert call[2] == publisher.stream_key(
        progress_event.tenant_id, progress_event.job_id
    )
    assert call[-2:] == (86400, 2000)


@pytest.mark.asyncio
async def test_duplicate_event_is_not_published_twice() -> None:
    publisher = RedisProgressPublisher(FakeRedis(result=None), settings())
    assert await publisher.publish(event()) is None


def test_dedup_key_does_not_expose_raw_business_key() -> None:
    key = RedisProgressPublisher._dedup_key(
        "stream", "tenant-secret:job:research"
    )
    assert "tenant-secret" not in key
    assert len(key.removeprefix("stream:dedup:")) == 64


@pytest.mark.asyncio
async def test_redis_failure_is_retryable() -> None:
    redis = FakeRedis()
    redis.error = ConnectionError("redis unavailable")
    publisher = RedisProgressPublisher(redis, settings())

    with pytest.raises(ProgressPublishError) as error:
        await publisher.publish(event())
    assert error.value.retryable is True
