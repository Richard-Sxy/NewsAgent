"""Bounded aggregate cache and single-flight requests, without reader identities."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import math
import re
from uuid import UUID

from pydantic import ValidationError

from app.audience.taxonomy import TopicCatalog
from app.reader_profiles.contracts import (
    NewsReaderProfilePort, READER_DEFINITION, ReaderProfileBatch,
    ReaderProfileProviderError, ReaderProfileQuery, canonical_json,
    reader_profile_query_fingerprint,
)


_LOCAL_ENVIRONMENTS = frozenset({"local", "development", "test", "e2e"})
_ENVIRONMENTS = _LOCAL_ENVIRONMENTS | {"staging", "production", "prod"}
_SERVICE_POLICY = "reader-profile-service-validated-causal-batch-v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _aware_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Reader profile clock must be timezone aware")
    return value


def validate_reader_profile_batch(query: ReaderProfileQuery, raw: ReaderProfileBatch, *,
                                  client: NewsReaderProfilePort, now: datetime,
                                  catalog: TopicCatalog | None = None,
                                  environment: str | None = None) -> ReaderProfileBatch:
    """Revalidate even internally-constructed models before exposing their aggregates."""
    now = _aware_now(now)
    try:
        batch = ReaderProfileBatch.model_validate(raw.model_dump(mode="json"))
        expected_mode = client.expected_source_mode
        configured_environment = environment if environment is not None else getattr(client, "environment", None)
        if (configured_environment not in _ENVIRONMENTS
                or expected_mode not in {"enterprise", "synthetic"}
                or (expected_mode == "synthetic" and configured_environment not in _LOCAL_ENVIRONMENTS)
                or query.source_release_sha256 != client.source_release_sha256
                or batch.source_release_sha256 != client.source_release_sha256
                or batch.tenant_id != query.tenant_id
                or (batch.window_start, batch.window_end) != (query.window_start, query.window_end)
                or batch.query_fingerprint != reader_profile_query_fingerprint(query)
                or {item.news_id for item in batch.items} != set(query.news_ids)
                or batch.taxonomy_version != client.expected_taxonomy_version
                or batch.profile_policy_version != client.expected_profile_policy_version
                or batch.source_mode != expected_mode
                or not batch.is_complete
                or not query.window_end <= batch.watermark <= batch.generated_at <= now
                or (batch.expires_at is not None and batch.expires_at <= now)
                or any(not item.trigger_article_excluded or item.profile_cutoff > query.window_start
                       or item.reader_definition != READER_DEFINITION for item in batch.items)):
            raise ValueError("invalid reader snapshot binding")
        if catalog is not None:
            if (catalog.taxonomy_version != batch.taxonomy_version
                    or (expected_mode == "enterprise" and catalog.status != "approved")):
                raise ValueError("invalid reader catalog binding")
            for item in batch.items:
                catalog.validate_ids(item.content_topic_ids)
                for interest in (item.long_term, item.short_term):
                    catalog.validate_ids(tuple(topic.topic_id for topic in interest.topics))
        return batch
    except (ValueError, TypeError, AttributeError, ValidationError, OverflowError):
        raise ReaderProfileProviderError("invalid_response") from None


class ReaderProfileService:
    def __init__(self, client: NewsReaderProfilePort, *, catalog: TopicCatalog,
                 timeout_seconds: float = 5, cache_ttl_seconds: float = 30,
                 max_cache_entries: int = 256, max_inflight: int = 16,
                 environment: str | None = None, clock=None) -> None:
        for value, maximum, allow_zero in ((timeout_seconds, 120, False), (cache_ttl_seconds, 300, True)):
            if (type(value) not in {int, float} or not math.isfinite(value)
                    or not (0 <= value <= maximum if allow_zero else 0 < value <= maximum)):
                raise ValueError("Invalid reader profile service time budget")
        if type(max_cache_entries) is not int or not 1 <= max_cache_entries <= 4096:
            raise ValueError("Invalid reader profile cache capacity")
        if type(max_inflight) is not int or not 1 <= max_inflight <= 256:
            raise ValueError("Invalid reader profile concurrency capacity")
        configured_environment = environment if environment is not None else getattr(client, "environment", None)
        if (configured_environment not in _ENVIRONMENTS
                or client.expected_source_mode not in {"enterprise", "synthetic"}
                or (client.expected_source_mode == "synthetic" and configured_environment not in _LOCAL_ENVIRONMENTS)):
            raise ValueError("Invalid reader profile source environment")
        if (not _HEX64.fullmatch(client.identity_sha256)
                or not _HEX64.fullmatch(client.source_release_sha256)
                or catalog.taxonomy_version != client.expected_taxonomy_version
                or (client.expected_source_mode == "enterprise" and catalog.status != "approved")):
            raise ValueError("Invalid reader profile service identity or catalog")
        self.client = client
        self.catalog = catalog
        self.environment = configured_environment
        self.source_release_sha256 = client.source_release_sha256
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_cache_entries = max_cache_entries
        self.max_inflight = max_inflight
        self.clock = clock or (lambda: datetime.now(UTC))
        identity = {"service_policy_version": _SERVICE_POLICY, "client_identity_sha256": client.identity_sha256,
                    "source_release_sha256": self.source_release_sha256,
                    "catalog_sha256": sha256(canonical_json(catalog.model_dump(mode="json")).encode()).hexdigest(),
                    "environment": self.environment}
        self._identity_sha256 = sha256(canonical_json(identity).encode()).hexdigest()
        self._cache: OrderedDict[str, tuple[datetime, ReaderProfileBatch]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[ReaderProfileBatch]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    def context_identity_for_window(self, *, tenant_id: UUID | str,
                                    window_start: datetime, window_end: datetime) -> str:
        """Bind the run before candidate news IDs are known; batch queries bind IDs later."""
        try:
            window = ReaderProfileQuery(tenant_id=tenant_id, news_ids=("context-window",),
                window_start=window_start, window_end=window_end,
                source_release_sha256=self.source_release_sha256)
        except (ValidationError, ValueError, TypeError):
            raise ReaderProfileProviderError("invalid_query") from None
        identity = {"service_identity_sha256": self.identity_sha256,
                    "source_release_sha256": self.source_release_sha256,
                    "tenant_id": str(window.tenant_id),
                    "window_start": window.window_start.astimezone(UTC).isoformat(),
                    "window_end": window.window_end.astimezone(UTC).isoformat()}
        return sha256(canonical_json(identity).encode()).hexdigest()

    async def fetch(self, query: ReaderProfileQuery) -> ReaderProfileBatch:
        try:
            query = ReaderProfileQuery.model_validate(query.model_dump(mode="json"))
        except (ValueError, TypeError, AttributeError, ValidationError):
            raise ReaderProfileProviderError("invalid_query") from None
        if query.source_release_sha256 != self.source_release_sha256:
            raise ReaderProfileProviderError("invalid_query")
        key = reader_profile_query_fingerprint(query)
        now = _aware_now(self.clock())
        if query.window_end > now:
            raise ReaderProfileProviderError("invalid_query")
        async with self._lock:
            if self._closed:
                raise ReaderProfileProviderError("closed")
            cached = self._cache.get(key)
            if cached is not None:
                expiry, batch = cached
                if expiry > now:
                    validated = self._validate(query, batch, now)
                    self._cache.move_to_end(key)
                    return validated
                del self._cache[key]
            task = self._inflight.get(key)
            if task is None:
                if len(self._inflight) >= self.max_inflight:
                    raise ReaderProfileProviderError("busy", retryable=True)
                task = asyncio.create_task(self._fetch_and_cache(key, query))
                # Consume unobserved failures if every waiting HTTP request is cancelled.
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                self._inflight[key] = task
        try:
            # Cancelling one waiter must not cancel another caller's shared request.
            async with asyncio.timeout(self.timeout_seconds):
                return await asyncio.shield(task)
        except TimeoutError:
            raise ReaderProfileProviderError("timeout", retryable=True) from None

    def _validate(self, query, batch, now):
        return validate_reader_profile_batch(query, batch, client=self.client, now=now,
            catalog=self.catalog, environment=self.environment)

    async def _fetch_and_cache(self, key: str, query: ReaderProfileQuery) -> ReaderProfileBatch:
        try:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    raw = await self.client.fetch(query)
                    now = _aware_now(self.clock())
                    batch = self._validate(query, raw, now)
            except TimeoutError:
                raise ReaderProfileProviderError("timeout", retryable=True) from None
            expiry = now + timedelta(seconds=self.cache_ttl_seconds)
            if batch.expires_at is not None:
                expiry = min(expiry, batch.expires_at)
            async with self._lock:
                if not self._closed and expiry > _aware_now(self.clock()):
                    self._cache[key] = (expiry, batch)
                    self._cache.move_to_end(key)
                    while len(self._cache) > self.max_cache_entries:
                        self._cache.popitem(last=False)
            return batch
        except ReaderProfileProviderError:
            raise
        except (OSError, ConnectionError):
            raise ReaderProfileProviderError("unavailable", retryable=True) from None
        except Exception:
            # A provider implementation must not leak raw payloads through exception text.
            raise ReaderProfileProviderError("unavailable", retryable=True) from None
        finally:
            async with self._lock:
                if self._inflight.get(key) is asyncio.current_task():
                    self._inflight.pop(key, None)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = tuple(self._inflight.values())
            self._cache.clear()
            self._inflight.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.close()
