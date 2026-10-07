import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from redis.exceptions import ConnectionError

from app.api.events import format_sse
from app.domain.execution import StepType
from app.domain.job_status import JobStatus
from app.schemas.events import ProgressEvent
from app.services.event_reader import (
    InvalidStreamEventID,
    ProgressReadError,
    RedisProgressReader,
)


class FakeRedis:
    def __init__(self, response=None) -> None:
        self.response = response or []
        self.calls = []
        self.error = None

    async def xread(self, streams, **kwargs):
        self.calls.append((streams, kwargs))
        if self.error:
            raise self.error
        return self.response


def settings():
    return SimpleNamespace(
        progress_stream_prefix="news-writing:events",
        sse_block_ms=15000,
        sse_batch_size=100,
    )


def payload(tenant_id, job_id):
    return {
        "event": "step.completed",
        "deduplication_key": "job:research:1:completed",
        "tenant_id": str(tenant_id),
        "job_id": str(job_id),
        "step": "research",
        "status": "research_review",
        "progress": {"completed": 1, "total": 1, "percent": 15},
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.asyncio
async def test_reader_decodes_stream_event_and_assigns_id() -> None:
    tenant_id, job_id = uuid.uuid4(), uuid.uuid4()
    stream = f"news-writing:events:{tenant_id}:{job_id}".encode()
    redis = FakeRedis(
        [(stream, [(b"1720000000000-1", {b"payload": json.dumps(payload(tenant_id, job_id)).encode()})])]
    )
    reader = RedisProgressReader(redis, settings())

    events = await reader.read(tenant_id, job_id, "1720000000000-0")

    assert len(events) == 1
    assert events[0].event_id == "1720000000000-1"
    streams, options = redis.calls[0]
    assert streams[reader.stream_key(tenant_id, job_id)] == "1720000000000-0"
    assert options == {"count": 100, "block": 15000}


def test_last_event_id_is_strictly_validated() -> None:
    assert RedisProgressReader.validate_event_id(None) == "0-0"
    assert RedisProgressReader.validate_event_id("0-0") == "0-0"
    with pytest.raises(InvalidStreamEventID):
        RedisProgressReader.validate_event_id("0-0\nX-Injected: true")


@pytest.mark.asyncio
async def test_malformed_historical_event_is_skipped() -> None:
    tenant_id, job_id = uuid.uuid4(), uuid.uuid4()
    redis = FakeRedis(
        [(b"stream", [(b"1-0", {b"payload": b"not-json"})])]
    )
    reader = RedisProgressReader(redis, settings())
    assert await reader.read(tenant_id, job_id, "0-0") == []


@pytest.mark.asyncio
async def test_redis_read_error_is_retryable() -> None:
    redis = FakeRedis()
    redis.error = ConnectionError("unavailable")
    reader = RedisProgressReader(redis, settings())
    with pytest.raises(ProgressReadError) as error:
        await reader.read(uuid.uuid4(), uuid.uuid4(), "$")
    assert error.value.retryable is True


def test_sse_format_contains_id_event_retry_and_single_json_data_line() -> None:
    event = ProgressEvent(
        event="step.completed",
        event_id="10-1",
        deduplication_key="job:research:1:completed",
        tenant_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        step=StepType.RESEARCH,
        status=JobStatus.RESEARCH_REVIEW,
        progress={"completed": 1, "total": 1, "percent": 15},
        occurred_at=datetime.now(timezone.utc),
    )
    value = format_sse(event, 5000)
    assert value.startswith("id: 10-1\nevent: step.completed\nretry: 5000\n")
    assert value.count("data: ") == 1
    assert value.endswith("\n\n")
