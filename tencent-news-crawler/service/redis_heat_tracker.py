"""基于 Redis 的热度衰减计数（生产实现）。

设计要点
--------
1. 热度按指数衰减：``H(t) = H0 * 2 ** (-Δt / tau_half)``。
2. 读改写用 Lua 原子完成，避免并发下丢失更新。
3. 时间统一取 Redis 服务器时间，避免多实例客户端时钟漂移。
4. key 带 TTL，长期无人访问自动过期，冷数据无需手动清理。
5. 支持租户隔离；批量读取在 Redis Cluster 下用 pipeline 逐 key，避免跨 slot。
6. 只维护「被访问过」的条目，未访问过的新闻不占任何 Redis 内存。

依赖：``redis>=5``（``redis.asyncio``）。

用法::

    from redis.asyncio import Redis

    redis = Redis.from_url("redis://...", decode_responses=True)
    tracker = RedisDecayedHeatTracker(redis, tau_half_days=7.0)

    await tracker.touch(tenant_id="t1", news_id="n1", weight=3.0)  # 一次点击
    heat = await tracker.get(tenant_id="t1", news_id="n1")
    heats = await tracker.get_many(tenant_id="t1", news_ids=["n1", "n2"])
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from redis.asyncio import Redis
from redis.exceptions import RedisError


_SECONDS_PER_DAY = 86400.0

# 读改写 + 刷新 TTL，原子执行；返回新的热度值。
# KEYS[1] = 热度 key
# ARGV[1] = 当前时间（空串表示使用 Redis 服务器 TIME）
# ARGV[2] = 本次访问权重
# ARGV[3] = 半衰期（秒）
# ARGV[4] = TTL（秒）
_TOUCH_LUA = """
local key = KEYS[1]
local now
if ARGV[1] == '' then
  local t = redis.call('TIME')
  now = tonumber(t[1]) + tonumber(t[2]) / 1000000
else
  now = tonumber(ARGV[1])
end
local weight = tonumber(ARGV[2])
local tau = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local h = tonumber(redis.call('HGET', key, 'h') or '0')
local ts = tonumber(redis.call('HGET', key, 'ts') or now)
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
if h > 0 and elapsed > 0 then
  h = h * (0.5 ^ (elapsed / tau))
end
h = h + weight

redis.call('HSET', key, 'h', h, 'ts', now)
redis.call('EXPIRE', key, ttl)
return tostring(h)
"""

# 只读不写，按经过时间衰减后返回。
# KEYS[1] = 热度 key
# ARGV[1] = 当前时间（空串表示使用 Redis 服务器 TIME）
# ARGV[2] = 半衰期（秒）
_GET_LUA = """
local key = KEYS[1]
local raw = redis.call('HGET', key, 'h')
if not raw then
  return '0'
end
local now
if ARGV[1] == '' then
  local t = redis.call('TIME')
  now = tonumber(t[1]) + tonumber(t[2]) / 1000000
else
  now = tonumber(ARGV[1])
end
local tau = tonumber(ARGV[2])

local h = tonumber(raw)
local ts = tonumber(redis.call('HGET', key, 'ts') or now)
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
if h > 0 and elapsed > 0 then
  h = h * (0.5 ^ (elapsed / tau))
end
return tostring(h)
"""


class HeatTrackerError(RuntimeError):
    """热度读写失败。"""


class RedisDecayedHeatTracker:
    """生产级热度衰减计数器。

    :param redis: ``redis.asyncio.Redis``（或 RedisCluster）客户端，
        建议 ``decode_responses=True``。
    :param prefix: key 前缀，用于与其他业务隔离。
    :param tau_half_days: 热度半衰期（天）。新闻建议 7 天。
    :param ttl_half_lives: key 的 TTL 为多少个半衰期。建议 4（约保留最后 6% 热度）。
    :param now_fn: 仅测试注入；生产不传，时间取 Redis 服务器 TIME。
    """

    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str = "vector_heat",
        tau_half_days: float = 7.0,
        ttl_half_lives: float = 4.0,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        if tau_half_days <= 0:
            raise ValueError("tau_half_days must be greater than 0")
        if ttl_half_lives <= 0:
            raise ValueError("ttl_half_lives must be greater than 0")

        normalized_prefix = prefix.strip().strip(":")
        if not normalized_prefix:
            raise ValueError("prefix cannot be empty")

        self._redis = redis
        self._prefix = normalized_prefix
        self._tau_seconds = tau_half_days * _SECONDS_PER_DAY
        self._ttl_seconds = int(self._tau_seconds * ttl_half_lives)
        self._now_fn = now_fn
        # register_script 会自动处理 EVALSHA / NOSCRIPT 回退到 EVAL。
        self._touch_script = redis.register_script(_TOUCH_LUA)
        self._get_script = redis.register_script(_GET_LUA)

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    async def touch(
        self,
        *,
        tenant_id: str,
        news_id: str,
        weight: float = 1.0,
        now: float | None = None,
    ) -> float:
        """记录一次访问并返回更新后的热度。

        ``weight`` 建议：检索命中 1、点击 3、转交写作 5。
        ``now`` 仅测试注入；生产传 ``None``，由 Redis 服务器统一计时。
        """
        self._validate_identity(tenant_id=tenant_id, news_id=news_id)
        if weight <= 0:
            raise ValueError("weight must be greater than 0")

        args = [self._resolve_now(now), str(weight), str(self._tau_seconds), str(self._ttl_seconds)]
        try:
            raw = await self._touch_script(
                keys=[self._key(tenant_id=tenant_id, news_id=news_id)],
                args=args,
            )
        except RedisError as exc:
            raise HeatTrackerError(
                f"touch heat failed: tenant={tenant_id!r} news={news_id!r}"
            ) from exc
        return self._to_float(raw)

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    async def get(
        self,
        *,
        tenant_id: str,
        news_id: str,
        now: float | None = None,
    ) -> float:
        """读取当前热度（按经过时间衰减，不修改存储）。"""
        self._validate_identity(tenant_id=tenant_id, news_id=news_id)
        args = [self._resolve_now(now), str(self._tau_seconds)]
        try:
            raw = await self._get_script(
                keys=[self._key(tenant_id=tenant_id, news_id=news_id)],
                args=args,
            )
        except RedisError as exc:
            raise HeatTrackerError(
                f"get heat failed: tenant={tenant_id!r} news={news_id!r}"
            ) from exc
        return self._to_float(raw)

    async def get_many(
        self,
        *,
        tenant_id: str,
        news_ids: Iterable[str],
        now: float | None = None,
    ) -> dict[str, float]:
        """批量读取热度。

        使用 pipeline 逐 key 执行只读脚本，兼容 Redis Cluster
        （避免多 key 脚本跨 slot）。返回顺序与入参一致。
        """
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")

        unique_ids = list(dict.fromkeys(news_ids))
        if not unique_ids:
            return {}
        for news_id in unique_ids:
            if not news_id.strip():
                raise ValueError("news_ids cannot contain empty values")

        resolved_now = self._resolve_now(now)
        args = [resolved_now, str(self._tau_seconds)]
        try:
            pipe = self._redis.pipeline(transaction=False)
            for news_id in unique_ids:
                self._get_script(
                    keys=[self._key(tenant_id=tenant_id, news_id=news_id)],
                    args=args,
                    client=pipe,
                )
            results = await pipe.execute()
        except RedisError as exc:
            raise HeatTrackerError(
                f"get_many heat failed: tenant={tenant_id!r}"
            ) from exc
        return {
            news_id: self._to_float(value)
            for news_id, value in zip(unique_ids, results, strict=True)
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _key(self, *, tenant_id: str, news_id: str) -> str:
        return f"{self._prefix}:{tenant_id}:{news_id}"

    def _resolve_now(self, now: float | None) -> str:
        """返回 Lua 的时间参数：空串 = 用 Redis 服务器时间。"""
        if now is not None:
            return repr(float(now))
        if self._now_fn is not None:
            return repr(float(self._now_fn()))
        return ""

    @staticmethod
    def _to_float(value: object) -> float:
        """兼容 decode_responses 为 True（str）或 False（bytes）。"""
        if isinstance(value, bytes):
            return float(value.decode())
        return float(value)  # type: ignore[arg-type]

    @staticmethod
    def _validate_identity(*, tenant_id: str, news_id: str) -> None:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        if not news_id.strip():
            raise ValueError("news_id cannot be empty")
        # 避免拼接 key 时出现分隔符污染（tenant/news_id 不应含冒号）。
        if ":" in tenant_id or ":" in news_id:
            raise ValueError("tenant_id and news_id must not contain ':'")


__all__ = ["HeatTrackerError", "RedisDecayedHeatTracker"]
