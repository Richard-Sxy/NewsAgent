import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from redis.exceptions import ConnectionError

from app.api.hot_news import format_hot_news_sse
from app.api.dependencies import (
    get_data_loop_gateway_token,
    get_hot_news_event_stream,
)
from app.main import create_app
from app.schemas.hot_news_events import HotNewsProgressEvent
from app.services.hot_news_event_stream import (
    HotNewsProgressPublishError,
    HotNewsProgressReadError,
    InvalidHotNewsStreamEventID,
    RedisHotNewsEventStream,
)


NOW = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
RUN_KEY = "hot-news-" + "a" * 64


class FakeRedis:
    def __init__(self, *, publish_result=b"1720000000000-0") -> None:
        self.publish_result = publish_result
        self.read_result = []
        self.eval_calls = []
        self.xread_calls = []
        self.error = None

    async def eval(self, *args):
        self.eval_calls.append(args)
        if self.error is not None:
            raise self.error
        return self.publish_result

    async def xread(self, streams, **kwargs):
        self.xread_calls.append((streams, kwargs))
        if self.error is not None:
            raise self.error
        return self.read_result


def settings():
    return SimpleNamespace(
        hot_news_stream_prefix="news-agent:hot-news:events",
        hot_news_stream_maxlen=1000,
        hot_news_stream_ttl_seconds=259200,
        hot_news_stream_dedup_ttl_seconds=259200,
        hot_news_stream_publish_timeout_seconds=2.0,
        sse_block_ms=15000,
        sse_batch_size=100,
    )


def event(**updates) -> HotNewsProgressEvent:
    values = {
        "event": "hot-news.run.completed",
        "deduplication_key": f"{RUN_KEY}:completed",
        "tenant_id": "tenant-1",
        "run_key": RUN_KEY,
        "run_id": "run-1",
        "status": "completed",
        "production_bundle_version": "bundle-v1",
        "workflow_version": "hot-news-workflow-v1",
        "window_start": NOW,
        "window_end": NOW + timedelta(hours=1),
        "counts": {
            "fetched_record_count": 100,
            "metric_snapshot_count": 10,
            "ranked_news_count": 5,
            "analyzed_news_count": 5,
        },
        "occurred_at": NOW + timedelta(hours=1),
    }
    values.update(updates)
    return HotNewsProgressEvent.model_validate(values)


@pytest.mark.asyncio
async def test_publish_is_atomic_bounded_and_expiring() -> None:
    redis = FakeRedis()
    stream = RedisHotNewsEventStream(redis, settings())

    published = await stream.publish(event())

    assert published is not None
    assert published.event_id == "1720000000000-0"
    call = redis.eval_calls[0]
    assert "XADD" in call[0]
    assert "EXPIRE" in call[0]
    assert call[1] == 2
    assert call[-3:] == (259200, 1000, 259200)


def test_stream_and_dedup_keys_share_one_cluster_slot() -> None:
    stream = RedisHotNewsEventStream(FakeRedis(), settings())
    stream_key = stream.stream_key("tenant-secret", RUN_KEY)
    dedup_key = stream._dedup_key(stream_key, f"{RUN_KEY}:completed")

    hash_tag = stream_key[stream_key.index("{") : stream_key.index("}") + 1]
    assert hash_tag in dedup_key
    assert "tenant-secret" not in stream_key
    assert RUN_KEY not in stream_key


@pytest.mark.asyncio
async def test_duplicate_event_is_suppressed() -> None:
    stream = RedisHotNewsEventStream(
        FakeRedis(publish_result=None),
        settings(),
    )
    assert await stream.publish(event()) is None


@pytest.mark.asyncio
async def test_read_validates_payload_and_sets_stream_id() -> None:
    redis = FakeRedis()
    stream = RedisHotNewsEventStream(redis, settings())
    redis.read_result = [
        (
            stream.stream_key("tenant-1", RUN_KEY).encode(),
            [
                (
                    b"1720000000000-1",
                    {
                        b"payload": event().model_dump_json(
                            exclude={"event_id"}
                        ).encode()
                    },
                )
            ],
        )
    ]

    events = await stream.read(
        tenant_id="tenant-1",
        run_key=RUN_KEY,
        last_event_id="0-0",
    )

    assert [item.event_id for item in events] == ["1720000000000-1"]
    streams, options = redis.xread_calls[0]
    assert streams[stream.stream_key("tenant-1", RUN_KEY)] == "0-0"
    assert options == {"count": 100, "block": 15000}


@pytest.mark.asyncio
async def test_malformed_or_cross_scope_payload_is_skipped() -> None:
    redis = FakeRedis()
    stream = RedisHotNewsEventStream(redis, settings())
    wrong_scope = event(tenant_id="tenant-2").model_dump_json(
        exclude={"event_id"}
    )
    redis.read_result = [
        (
            b"stream",
            [
                (b"1-0", {b"payload": b"not-json"}),
                (b"2-0", {b"payload": wrong_scope.encode()}),
            ],
        )
    ]

    assert await stream.read(
        tenant_id="tenant-1",
        run_key=RUN_KEY,
        last_event_id="0-0",
    ) == []


def test_last_event_id_is_strictly_validated() -> None:
    assert RedisHotNewsEventStream.validate_event_id(None) == "0-0"
    assert RedisHotNewsEventStream.validate_event_id("12-3") == "12-3"
    with pytest.raises(InvalidHotNewsStreamEventID):
        RedisHotNewsEventStream.validate_event_id("0-0\nInjected: true")


@pytest.mark.asyncio
async def test_redis_errors_are_retryable_at_adapter_boundary() -> None:
    redis = FakeRedis()
    redis.error = ConnectionError("redis unavailable")
    stream = RedisHotNewsEventStream(redis, settings())

    with pytest.raises(HotNewsProgressPublishError) as publish_error:
        await stream.publish(event())
    assert publish_error.value.retryable is True

    with pytest.raises(HotNewsProgressReadError) as read_error:
        await stream.read(
            tenant_id="tenant-1",
            run_key=RUN_KEY,
            last_event_id="0-0",
        )
    assert read_error.value.retryable is True


def test_sse_format_contains_cursor_type_and_one_json_line() -> None:
    value = format_hot_news_sse(
        event(event_id="10-1"),
        retry_ms=5000,
    )
    assert value.startswith(
        "id: 10-1\nevent: hot-news.run.completed\nretry: 5000\n"
    )
    assert value.count("data: ") == 1
    assert json.loads(value.split("data: ", 1)[1].strip())["run_key"] == RUN_KEY


@pytest.mark.asyncio
async def test_sse_endpoint_rejects_invalid_cursor_after_auth() -> None:
    app = create_app()

    async def gateway_token() -> str:
        return "test-token"

    async def event_stream():
        return SimpleNamespace(
            validate_event_id=RedisHotNewsEventStream.validate_event_id
        )

    app.dependency_overrides[get_data_loop_gateway_token] = gateway_token
    app.dependency_overrides[get_hot_news_event_stream] = event_stream
    headers = {
        "Authorization": "Bearer test-token",
        "X-Tenant-ID": str(uuid4()),
        "X-User-ID": str(uuid4()),
        "X-Hot-News-Roles": "hot-news:read",
        "Last-Event-ID": "0-0\nInjected: true",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/hot-news/streams/{RUN_KEY}/events",
            headers=headers,
        )

    assert response.status_code == 400
