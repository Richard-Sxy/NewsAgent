"""Isolated aggregate fixture; it never reads, stores or returns real users."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Callable

from app.modules.audience.contracts import (
    AudienceProviderError,
    AudienceQuery,
    AudienceSnapshot,
    audience_query_fingerprint,
)


LOCAL_AUDIENCE_ENVIRONMENTS = frozenset({"local", "development", "test", "e2e"})


class LocalAudienceProfileClient:
    def __init__(self, catalog, *, environment: str,
                 clock: Callable[[], datetime] | None = None,
                 ttl_seconds: int = 300) -> None:
        if environment not in LOCAL_AUDIENCE_ENVIRONMENTS:
            raise ValueError("Synthetic audiences require an isolated local environment")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("Synthetic audience TTL must be within 1 to 300 seconds")
        self._catalog = catalog
        self._ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._closed = False
        self._groups: dict[str, set[int]] = {topic.topic_id: set() for topic in catalog.topics}
        # Integers are temporary fixture set positions, not recipient identities.
        # Every descendant cohort is included in each of its ancestor topics.
        for topic in catalog.topics:
            if not topic.enabled:
                continue
            salt = int(sha256(topic.topic_id.encode("utf-8")).hexdigest()[:8], 16)
            positions = {position for position in range(240)
                         if position < 12 or (position + salt) % 11 < 4}
            for ancestor in catalog.ancestor_ids(topic.topic_id):
                self._groups[ancestor].update(positions)

    async def close(self) -> None:
        self._closed = True

    async def estimate(self, query: AudienceQuery) -> AudienceSnapshot:
        if self._closed:
            raise AudienceProviderError("closed")
        if query.selection.taxonomy_version != self._catalog.taxonomy_version:
            raise AudienceProviderError("invalid_query")
        try:
            self._catalog.validate_ids(query.selection.include_topic_ids)
            self._catalog.validate_ids(query.selection.exclude_topic_ids)
        except (ValueError, KeyError):
            raise AudienceProviderError("invalid_query") from None
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None or query.window_end > now:
            raise AudienceProviderError("invalid_query")
        groups = [self._groups[topic_id] for topic_id in query.selection.include_topic_ids]
        matched = (set.union(*groups) if query.selection.match == "any" else set.intersection(*groups))
        for topic_id in query.selection.exclude_topic_ids:
            matched.difference_update(self._groups[topic_id])
        # Consent/frequency restrictions are only illustrative synthetic filters.
        eligible = {position for position in matched if position % 5 != 0 and position % 7 != 0}
        fingerprint = audience_query_fingerprint(query)
        snapshot_key = sha256(f"{fingerprint}|{now.isoformat()}".encode("utf-8")).hexdigest()
        return AudienceSnapshot(
            tenant_id=query.tenant_id, run_id=query.run_id, news_id=query.news_id,
            taxonomy_version=query.selection.taxonomy_version, query_fingerprint=fingerprint,
            snapshot_id=f"synthetic-{snapshot_key}", profile_version="synthetic-interest-profiles-v1",
            source_version="synthetic-audience-v1", window_start=query.window_start,
            window_end=query.window_end, generated_at=now, expires_at=now + timedelta(seconds=self._ttl_seconds),
            watermark=query.window_end, is_complete=True, matched_users=len(matched),
            eligible_users=len(eligible), source_mode="synthetic",
        )
