import json
import re
import uuid

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.schemas.events import ProgressEvent


class InvalidStreamEventID(ValueError):
    pass


class ProgressReadError(RuntimeError):
    retryable = True


class RedisProgressReader:
    """从共享 Redis Stream 读取可断线续传的任务事件。"""

    _STREAM_ID_PATTERN = re.compile(r"^(?:\$|\d+-\d+)$")

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.stream_prefix = settings.progress_stream_prefix.strip(":")
        self.block_ms = settings.sse_block_ms
        self.batch_size = settings.sse_batch_size

    async def read(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        last_event_id: str,
    ) -> list[ProgressEvent]:
        cursor = self.validate_event_id(last_event_id)
        stream_key = self.stream_key(tenant_id, job_id)
        try:
            response = await self.redis.xread(
                {stream_key: cursor},
                count=self.batch_size,
                block=self.block_ms,
            )
        except RedisError as exc:
            raise ProgressReadError("读取任务进度 Stream 失败") from exc

        events: list[ProgressEvent] = []
        for _, entries in response:
            for stream_id, fields in entries:
                normalized = self._normalize_fields(fields)
                payload = normalized.get("payload")
                if payload is None:
                    continue
                try:
                    value = json.loads(payload)
                    value["event_id"] = self._decode(stream_id)
                    events.append(ProgressEvent.model_validate(value))
                except (json.JSONDecodeError, TypeError, ValidationError):
                    # Relay 已做 Schema 校验；这里跳过异常历史数据，避免断开 SSE。
                    continue
        return events

    def stream_key(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> str:
        return f"{self.stream_prefix}:{tenant_id}:{job_id}"

    @classmethod
    def validate_event_id(cls, value: str | None) -> str:
        candidate = value or "0-0"
        if not cls._STREAM_ID_PATTERN.fullmatch(candidate):
            raise InvalidStreamEventID("Last-Event-ID 不是合法 Redis Stream ID")
        return candidate

    @classmethod
    def _normalize_fields(cls, fields: dict) -> dict[str, str]:
        return {cls._decode(key): cls._decode(value) for key, value in fields.items()}

    @staticmethod
    def _decode(value) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
