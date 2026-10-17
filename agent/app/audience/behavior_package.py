"""Exact set operations over local synthetic profiles; only counts leave this adapter."""

from datetime import datetime, timedelta, timezone

from app.audience.contracts import AudienceProviderError, AudienceQuery, AudienceSnapshot, audience_query_fingerprint
from app.behavior_tags.runtime import LocalBehaviorRuntime


class BehaviorPackageAudienceClient:
    def __init__(self, runtime: LocalBehaviorRuntime, *, ttl_seconds: int = 300):
        self.runtime = runtime
        self.ttl_seconds = ttl_seconds
        self.closed = False

    async def estimate(self, query: AudienceQuery) -> AudienceSnapshot:
        if self.closed:
            raise AudienceProviderError("closed")
        try:
            if query.selection.taxonomy_version != self.runtime.policy.catalog.taxonomy_version:
                raise ValueError("taxonomy mismatch")
            self.runtime.policy.catalog.validate_ids(query.selection.include_topic_ids)
            self.runtime.policy.catalog.validate_ids(query.selection.exclude_topic_ids)
            as_of, profiles = self.runtime.profiles_at(tenant_id=str(query.tenant_id),
                window_start=query.window_start, window_end=query.window_end)
            if datetime.fromisoformat(as_of) != query.window_end:
                raise ValueError("audience preview requires an exact completed profile snapshot")
        except ValueError as exc:
            raise AudienceProviderError("invalid_query") from exc
        matched = eligible = 0
        for profile in profiles:
            # A one-off incidental exposure has zero interest; positive weights come from actions.
            topics = {topic for topic, score in profile["topic_weights"].items() if score >= 0.15}
            included = set(query.selection.include_topic_ids)
            fits = included <= topics if query.selection.match == "all" else bool(included & topics)
            if not fits or topics & set(query.selection.exclude_topic_ids):
                continue
            matched += 1
            eligible += int(profile["notification_allowed"] and not profile["frequency_capped"])
        now = datetime.now(timezone.utc)
        return AudienceSnapshot(tenant_id=query.tenant_id, run_id=query.run_id, news_id=query.news_id,
            taxonomy_version=query.selection.taxonomy_version, query_fingerprint=audience_query_fingerprint(query),
            snapshot_id="behavior-" + self.runtime.package_sha256[:20],
            profile_version=self.runtime.manifest["profile_version"], source_version=self.runtime.package_sha256,
            window_start=query.window_start, window_end=query.window_end, generated_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds), watermark=datetime.fromisoformat(as_of),
            is_complete=True, matched_users=matched, eligible_users=eligible, source_mode="synthetic")

    async def close(self) -> None:
        self.closed = True
