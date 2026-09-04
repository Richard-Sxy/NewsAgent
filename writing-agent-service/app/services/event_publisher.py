import hashlib

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.schemas.events import ProgressEvent


class ProgressPublishError(RuntimeError):
    """Redis 发布失败；由 Temporal 判断为可重试技术错误。"""

    retryable = True


class RedisProgressPublisher:
    """通过 Redis Stream 原子发布短期进度事件。"""

    _PUBLISH_SCRIPT = """
    if redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[2]) then
        return redis.call(
            'XADD', KEYS[1], 'MAXLEN', '~', ARGV[3],
            '*', 'payload', ARGV[1]
        )
    end
    return false
    """

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.stream_prefix = settings.progress_stream_prefix.strip(":")
        self.maxlen = settings.progress_stream_maxlen
        self.dedup_ttl = settings.progress_dedup_ttl_seconds

    async def publish(self, event: ProgressEvent) -> ProgressEvent | None:
        """发布成功返回带 Stream ID 的事件；重复事件返回 None。"""
        stream_key = self.stream_key(event.tenant_id, event.job_id)
        dedup_key = self._dedup_key(stream_key, event.deduplication_key)
        payload = event.model_dump_json(exclude={"event_id"})
        try:
            stream_id = await self.redis.eval(
                self._PUBLISH_SCRIPT,
                2,
                stream_key,
                dedup_key,
                payload,
                self.dedup_ttl,
                self.maxlen,
            )
        except RedisError as exc:
            raise ProgressPublishError(
                f"发布进度事件失败: {event.deduplication_key}"
            ) from exc
        if not stream_id:
            return None
        if isinstance(stream_id, bytes):
            stream_id = stream_id.decode("utf-8")
        return event.model_copy(update={"event_id": str(stream_id)})

    def stream_key(self, tenant_id, job_id) -> str:
        return f"{self.stream_prefix}:{tenant_id}:{job_id}"

    @staticmethod
    def _dedup_key(stream_key: str, deduplication_key: str) -> str:
        digest = hashlib.sha256(deduplication_key.encode("utf-8")).hexdigest()
        return f"{stream_key}:dedup:{digest}"
