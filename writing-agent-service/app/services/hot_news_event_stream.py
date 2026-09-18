"""Redis Stream-backed transient events for hot-news runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.schemas.hot_news_events import HotNewsProgressEvent


class InvalidHotNewsStreamEventID(ValueError):
    """The SSE cursor is not a valid Redis Stream ID."""


class HotNewsProgressPublishError(RuntimeError):
    """Publishing failed; callers may degrade without losing business state."""

    retryable = True


class HotNewsProgressReadError(RuntimeError):
    """Reading failed; an SSE client can reconnect with the same cursor."""

    retryable = True


class RedisHotNewsEventStream:
    """Publish and read bounded hot-news progress streams.

    PostgreSQL remains the source of truth. Stream keys use one opaque Redis
    Cluster hash tag so the atomic deduplication script stays in one slot.
    """

    _STREAM_ID_PATTERN = re.compile(r"^(?:\$|\d+-\d+)$")
    _PUBLISH_SCRIPT = """
    if redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[2]) then
        local stream_id = redis.call(
            'XADD', KEYS[1], 'MAXLEN', '~', ARGV[3],
            '*', 'payload', ARGV[1]
        )
        redis.call('EXPIRE', KEYS[1], ARGV[4])
        return stream_id
    end
    return false
    """

    def __init__(self, redis: Redis, settings: Settings) -> None:
        prefix = settings.hot_news_stream_prefix.strip(":")
        if not prefix:
            raise ValueError("hot_news_stream_prefix cannot be empty")
        self.redis = redis
        self.stream_prefix = prefix
        self.maxlen = settings.hot_news_stream_maxlen
        self.stream_ttl = settings.hot_news_stream_ttl_seconds
        self.dedup_ttl = settings.hot_news_stream_dedup_ttl_seconds
        self.publish_timeout = (
            settings.hot_news_stream_publish_timeout_seconds
        )
        self.block_ms = settings.sse_block_ms
        self.batch_size = settings.sse_batch_size

    async def publish(
        self,
        event: HotNewsProgressEvent,
    ) -> HotNewsProgressEvent | None:
        stream_key = self.stream_key(event.tenant_id, event.run_key)
        dedup_key = self._dedup_key(
            stream_key,
            event.deduplication_key,
        )
        payload = event.model_dump_json(exclude={"event_id"})
        try:
            async with asyncio.timeout(self.publish_timeout):
                stream_id = await self.redis.eval(
                    self._PUBLISH_SCRIPT,
                    2,
                    stream_key,
                    dedup_key,
                    payload,
                    self.dedup_ttl,
                    self.maxlen,
                    self.stream_ttl,
                )
        except (RedisError, TimeoutError) as exc:
            raise HotNewsProgressPublishError(
                f"failed to publish hot-news event: {event.event}"
            ) from exc
        if not stream_id:
            return None
        return event.model_copy(update={"event_id": self._decode(stream_id)})

    async def read(
        self,
        *,
        tenant_id: str,
        run_key: str,
        last_event_id: str,
    ) -> list[HotNewsProgressEvent]:
        cursor = self.validate_event_id(last_event_id)
        stream_key = self.stream_key(tenant_id, run_key)
        try:
            response = await self.redis.xread(
                {stream_key: cursor},
                count=self.batch_size,
                block=self.block_ms,
            )
        except RedisError as exc:
            raise HotNewsProgressReadError(
                "failed to read hot-news progress stream"
            ) from exc

        events: list[HotNewsProgressEvent] = []
        for _, entries in response:
            for stream_id, fields in entries:
                normalized = {
                    self._decode(key): self._decode(value)
                    for key, value in fields.items()
                }
                payload = normalized.get("payload")
                if payload is None:
                    continue
                try:
                    value = json.loads(payload)
                    value["event_id"] = self._decode(stream_id)
                    event = HotNewsProgressEvent.model_validate(value)
                except (json.JSONDecodeError, TypeError, ValidationError):
                    continue
                if event.tenant_id != tenant_id or event.run_key != run_key:
                    continue
                events.append(event)
        return events

    def stream_key(self, tenant_id: str, run_key: str) -> str:
        identity = "\x1f".join((tenant_id, run_key))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{self.stream_prefix}:{{{digest}}}"

    @staticmethod
    def _dedup_key(stream_key: str, deduplication_key: str) -> str:
        digest = hashlib.sha256(
            deduplication_key.encode("utf-8")
        ).hexdigest()
        return f"{stream_key}:dedup:{digest}"

    @classmethod
    def validate_event_id(cls, value: str | None) -> str:
        candidate = value or "0-0"
        if not cls._STREAM_ID_PATTERN.fullmatch(candidate):
            raise InvalidHotNewsStreamEventID(
                "Last-Event-ID is not a valid Redis Stream ID"
            )
        return candidate

    @staticmethod
    def _decode(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)
