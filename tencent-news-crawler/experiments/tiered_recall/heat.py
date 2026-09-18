"""热度计数：指数半衰期衰减 H <- H * 2^(-elapsed / tau) + weight。

默认内存后端；生产可换成 Redis(Lua/FCALL)，接口保持一致。
"""

from __future__ import annotations

import time
from typing import Iterable

TOUCH_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
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


class HeatTracker:
    def __init__(self, tau_seconds: float = 86400.0, ttl_seconds: float = 2592000.0):
        if tau_seconds <= 0:
            raise ValueError("tau_seconds must be > 0")
        self.tau_seconds = float(tau_seconds)
        self.ttl_seconds = float(ttl_seconds)
        self._store: dict[str, tuple[float, float]] = {}

    @staticmethod
    def _now(now: float | None) -> float:
        return time.time() if now is None else float(now)

    def _decay(self, heat: float, elapsed: float) -> float:
        if heat <= 0 or elapsed <= 0:
            return heat
        return heat * (0.5 ** (elapsed / self.tau_seconds))

    def touch(self, news_id: str, weight: float = 1.0, now: float | None = None) -> float:
        if weight <= 0:
            raise ValueError("weight must be > 0")
        t = self._now(now)
        heat, ts = self._store.get(news_id, (0.0, t))
        heat = self._decay(heat, t - ts) + weight
        self._store[news_id] = (heat, t)
        return heat

    def get(self, news_id: str, now: float | None = None) -> float:
        if news_id not in self._store:
            return 0.0
        heat, ts = self._store[news_id]
        return self._decay(heat, self._now(now) - ts)

    def get_many(self, news_ids: Iterable[str], now: float | None = None) -> dict[str, float]:
        return {n: self.get(n, now=now) for n in news_ids}

    def snapshot(self) -> dict[str, tuple[float, float]]:
        return dict(self._store)

    def load(self, data: dict[str, list[float]]) -> None:
        self._store = {k: (float(v[0]), float(v[1])) for k, v in data.items()}
