"""Redis-wide inference admission. No model text, credentials or identities in Redis.

The policy reserves concurrency and rolling-minute requests for interactive scenes.
It is admission control, not a FIFO queue, token quota or provider-side cancellation.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError
import yaml

from app.model_runtime.http import ModelTransportError
from app.observability.telemetry import operation, record_error


class GovernancePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    pool: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    redis_url_env: str = Field(default="REDIS_URL", pattern=r"^[A-Z][A-Z0-9_]*$")
    max_concurrent: int = Field(ge=2, le=1000)
    interactive_reserved: int = Field(ge=1)
    requests_per_minute: int = Field(ge=2, le=100000)
    interactive_rpm_reserved: int = Field(ge=1)
    wait_seconds: float = Field(default=10, gt=0, le=120)
    call_timeout_seconds: float = Field(default=120, gt=0, le=300)
    interactive_scenes: tuple[str, ...] = ("conversation", "conversation_memory", "query_understanding", "text2sql_assistant")

    @model_validator(mode="after")
    def capacity(self):
        if self.interactive_reserved >= self.max_concurrent:
            raise ValueError("background concurrency must remain positive")
        if self.interactive_rpm_reserved >= self.requests_per_minute:
            raise ValueError("background RPM must remain positive")
        if not self.interactive_scenes or any(not s.strip() for s in self.interactive_scenes):
            raise ValueError("interactive scenes must be explicit")
        return self


# All keys share a Redis Cluster hash slot. Redis TIME avoids replica clock skew.
# Policy mismatch fails closed instead of letting two deployment versions double capacity.
_ACQUIRE = """
local policy = redis.call('GET', KEYS[1])
if policy and policy ~= ARGV[1] then return -2 end
if not policy then redis.call('SET', KEYS[1], ARGV[1]) end
local clock = redis.call('TIME')
local now = clock[1]*1000 + math.floor(clock[2]/1000)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', now-60000)
redis.call('ZREMRANGEBYSCORE', KEYS[5], '-inf', now-60000)
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[4]) then return 0 end
if redis.call('ZCARD', KEYS[4]) >= tonumber(ARGV[6]) then return 0 end
if ARGV[3] == 'background' then
  if redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[5]) then return 0 end
  if redis.call('ZCARD', KEYS[5]) >= tonumber(ARGV[7]) then return 0 end
end
redis.call('ZADD', KEYS[2], now+tonumber(ARGV[8]), ARGV[2])
redis.call('ZADD', KEYS[4], now, ARGV[2])
if ARGV[3] == 'background' then
  redis.call('ZADD', KEYS[3], now+tonumber(ARGV[8]), ARGV[2])
  redis.call('ZADD', KEYS[5], now, ARGV[2])
end
for i=2,5 do redis.call('PEXPIRE', KEYS[i], math.max(tonumber(ARGV[8]),60000)+5000) end
return 1
"""
_RELEASE = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return 1
"""


def admission_error(kind):
    return ModelTransportError("model admission failed", retryable=False,
                               failure_stage="quota_admission", failure_kind=kind)


class RedisModelAdmission:
    def __init__(self, redis, policy: GovernancePolicy):
        self.redis, self.policy = redis, policy
        prefix = "newsagent:model:{" + policy.pool + "}:"
        self.keys = [prefix + suffix for suffix in ("policy", "active", "background", "rpm", "background-rpm")]
        self.fingerprint = sha256(json.dumps(policy.model_dump(), sort_keys=True).encode()).hexdigest()

    async def acquire(self, lane, *, call_seconds):
        if lane not in {"interactive", "background"}:
            raise ValueError("invalid admission lane")
        ticket = uuid4().hex
        deadline = monotonic() + self.policy.wait_seconds
        with operation("model.quota_wait." + lane, attributes={"newsagent.stage": "quota_admission"}):
            while True:
                p = self.policy
                try:
                    result = await self.redis.eval(_ACQUIRE, 5, *self.keys, self.fingerprint, ticket, lane,
                        p.max_concurrent, p.max_concurrent-p.interactive_reserved,
                        p.requests_per_minute, p.requests_per_minute-p.interactive_rpm_reserved,
                        math.ceil((call_seconds + 5) * 1000))
                except RedisError:
                    raise admission_error("quota_backend_unavailable") from None
                if result == -2:
                    raise admission_error("quota_policy_mismatch")
                if result == 1:
                    return ticket
                if monotonic() >= deadline:
                    raise admission_error("quota_wait_timeout")
                await asyncio.sleep(min(.05, max(0, deadline-monotonic())))

    async def release(self, ticket):
        # Never remove minute usage on release; failures and retries consume RPM too.
        try:
            async with asyncio.timeout(2):
                await self.redis.eval(_RELEASE, 2, self.keys[1], self.keys[2], ticket)
        except (RedisError, TimeoutError):
            # A failed cleanup cannot open capacity. The bounded lease will expire.
            with operation("model.quota_release") as span:
                record_error(span, admission_error("quota_release_failed"), "quota_admission")


class GovernedInference:
    def __init__(self, inner, admission: RedisModelAdmission):
        self.inner, self.admission = inner, admission

    async def complete(self, request):
        policy = self.admission.policy
        seconds = min(request.timeout_seconds, policy.call_timeout_seconds)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("finite inference timeout required")
        lane = "interactive" if request.scene in policy.interactive_scenes else "background"
        ticket = None
        try:
            # Queue time belongs to the caller's total budget.
            async with asyncio.timeout(seconds):
                ticket = await self.admission.acquire(lane, call_seconds=seconds)
                return await self.inner.complete(request)
        except TimeoutError:
            if ticket is None:
                raise admission_error("quota_wait_timeout") from None
            raise ModelTransportError("model call deadline exceeded", retryable=False,
                failure_stage="inference", failure_kind="model_deadline") from None
        finally:
            if ticket is not None:
                await self.admission.release(ticket)

    async def close(self):
        try:
            close = getattr(self.inner, "close", None)
            if close:
                await close()
        finally:
            await self.admission.redis.aclose()


def govern_inference(inner):
    """Explicit process opt-in; never load .env or silently fall back from Redis."""
    path = os.environ.get("MODEL_GOVERNANCE_CONFIG_PATH")
    if not path:
        return inner
    file = Path(path)
    if file.stat().st_size > 65536:
        raise ValueError("model governance policy too large")
    policy = GovernancePolicy.model_validate(yaml.safe_load(file.read_text(encoding="utf-8")))
    url = os.environ.get(policy.redis_url_env)
    if not url:
        raise ValueError("model governance Redis environment variable missing")
    redis = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1, retry_on_timeout=False)
    return GovernedInference(inner, RedisModelAdmission(redis, policy))
