"""Indexed, read-only aggregate client for isolated synthetic reader packages."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
import sqlite3

from app.audience.taxonomy import TopicCatalog
from app.reader_profiles.build import (ALLOWED_ENVIRONMENTS, MAX_ARTIFACT_BYTES,
    MAX_POLICY_BYTES, PACKAGE_VERSION, POLICY_VERSION, _hash_file, _iso, _json,
    _loads, _manifest, _regular, _time)
from app.reader_profiles.contracts import (ReaderInterestSummary, ReaderProfileBatch,
    ReaderProfileProviderError, ReaderProfileQuery, ReaderProfileSummary,
    reader_profile_query_fingerprint)

_PRESENTATION_POLICY = "local-reader-prewindow-history-coverage-v2"


class LocalNewsReaderProfileClient:
    expected_source_mode = "synthetic"

    def __init__(self, package_path, *, environment):
        if environment not in ALLOWED_ENVIRONMENTS:
            raise ValueError("synthetic reader profiles require an isolated local environment")
        self.directory = Path(package_path).resolve(strict=True)
        manifest, raw_hash = _manifest(self.directory / "manifest.json")
        if manifest.get("package_version") != PACKAGE_VERSION or manifest.get("source_mode") != "synthetic" or manifest.get("private_rows_in_output") is not False:
            raise ValueError("unsupported or non-synthetic reader-profile package")
        artifacts = manifest.get("artifacts", {})
        if set(artifacts) != {"policy.json", "reader_profiles.sqlite"}:
            raise ValueError("reader-profile package artifacts are incomplete")
        for name, spec in artifacts.items():
            path = self.directory / name
            _regular(path, MAX_POLICY_BYTES if name == "policy.json" else MAX_ARTIFACT_BYTES)
            if path.stat().st_size != spec.get("bytes") or _hash_file(path) != spec.get("sha256"):
                raise ValueError("reader-profile artifact size or hash mismatch")
        self.policy = _loads((self.directory / "policy.json").read_bytes())
        self.catalog = TopicCatalog.model_validate(self.policy.get("catalog"))
        if (self.policy.get("policy_version") != POLICY_VERSION
                or self.policy.get("policy_version") != manifest.get("profile_policy_version")
                or self.catalog.taxonomy_version != manifest.get("taxonomy_version")
                or self.policy.get("taxonomy_version") != manifest.get("taxonomy_version")
                or self.policy.get("source_mode") != "synthetic"
                or self.policy.get("trigger_news_excluded") is not True
                or self.policy.get("window_hours") != manifest.get("window_hours")
                or manifest.get("window_hours") not in {1, 24}):
            raise ValueError("reader-profile policy/version mismatch")
        start, end = _time(manifest["coverage_start"]), _time(manifest["coverage_end"])
        if start >= end or _time(manifest["source_watermark"]) < end or _time(manifest["generated_at"]) < _time(manifest["source_watermark"]):
            raise ValueError("reader-profile coverage/watermark mismatch")
        self.manifest = manifest
        self.source_release_sha256 = raw_hash
        self.expected_taxonomy_version = manifest["taxonomy_version"]
        self.expected_profile_policy_version = manifest["profile_policy_version"]
        self.identity_sha256 = sha256(_json({"client": "local-news-reader-profile-v2", "release": raw_hash,
            "presentation_policy": _PRESENTATION_POLICY,
            "source_mode": "synthetic", "taxonomy_version": self.expected_taxonomy_version,
            "profile_policy_version": self.expected_profile_policy_version}).encode()).hexdigest()
        self._closed = False
        with self._connect() as connection:
            metadata = connection.execute("SELECT value FROM metadata WHERE key=?", ("manifest_core_json",)).fetchone()
            core = dict(manifest)
            core.pop("artifacts", None)
            core.pop("identity_sha256", None)
            if metadata is None or _loads(metadata[0]) != core:
                raise ValueError("reader-profile SQLite metadata identity mismatch")

    def _connect(self):
        connection = sqlite3.connect((self.directory / "reader_profiles.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        return connection

    def _empty(self, news, binding, query):
        return ReaderProfileSummary(news_id=news, source_mode="synthetic",
            taxonomy_version=self.expected_taxonomy_version,
            profile_policy_version=self.expected_profile_policy_version,
            content_sha256=binding.get("content_sha256"), content_topic_ids=tuple(binding.get("topic_ids", ())),
            reader_count=0, long_term=ReaderInterestSummary(lookback_hours=720, profiled_reader_count=0,
                unknown_reader_count=0, cold_start_reader_count=0, baseline_profiled_count=0),
            short_term=ReaderInterestSummary(lookback_hours=24, profiled_reader_count=0,
                unknown_reader_count=0, cold_start_reader_count=0, baseline_profiled_count=0),
            profile_cutoff=query.window_start, trigger_article_excluded=True,
            count_method="exact_synthetic_distinct", status="empty", error_bound=None,
            limitations=("synthetic readers only; not enterprise audience or delivery eligibility",
                "empty reader cohort; baseline not computed"))

    def _fetch_sync(self, query):
        if self._closed:
            raise ReaderProfileProviderError("closed")
        start, end = query.window_start, query.window_end
        if (query.source_release_sha256 != self.source_release_sha256
                or str(query.tenant_id) not in self.manifest["tenants"]
                or end - start != timedelta(hours=self.manifest["window_hours"])
                or any(value.minute or value.second or value.microsecond for value in (start, end))
                or start < _time(self.manifest["coverage_start"]) or end > _time(self.manifest["coverage_end"])):
            raise ReaderProfileProviderError("invalid_query")
        placeholders = ",".join("?" for _ in query.news_ids)
        tenant = str(query.tenant_id)
        with self._connect() as connection:
            row = connection.execute("SELECT payload_json FROM coverage WHERE tenant_id=? AND window_start=? AND window_end=?",
                (tenant, _iso(start), _iso(end))).fetchone()
            if row is None:
                raise ReaderProfileProviderError("invalid_query")
            coverage = _loads(row[0])
            if coverage.get("complete") is not True or coverage.get("source_mode") != "synthetic" or coverage.get("window_start") != _iso(start) or coverage.get("window_end") != _iso(end):
                raise ReaderProfileProviderError("invalid_response")
            bindings = {news: _loads(payload) for news, payload in connection.execute(
                "SELECT news_id,payload_json FROM contents WHERE tenant_id=? AND news_id IN (" + placeholders + ")",
                (tenant, *query.news_ids))}
            if set(bindings) != set(query.news_ids):
                # A complete event window only proves zero visits for known,
                # content-bound news. Missing content is missing data, not zero.
                raise ReaderProfileProviderError("invalid_query")
            payloads = {news: _loads(payload) for news, payload in connection.execute(
                "SELECT news_id,payload_json FROM reader_profiles WHERE tenant_id=? AND window_start=? AND window_end=? AND news_id IN (" + placeholders + ")",
                (tenant, _iso(start), _iso(end), *query.news_ids))}
        items = []
        for news in query.news_ids:
            binding = bindings.get(news, {})
            item = ReaderProfileSummary.model_validate(payloads[news]) if news in payloads else self._empty(news, binding, query)
            if (item.news_id != news or item.content_sha256 != binding.get("content_sha256")
                    or tuple(item.content_topic_ids) != tuple(binding.get("topic_ids", ()))
                    or item.profile_cutoff != start or not item.trigger_article_excluded):
                raise ReaderProfileProviderError("invalid_response")
            self.catalog.validate_ids(item.content_topic_ids)
            for dimension in (item.long_term, item.short_term):
                self.catalog.validate_ids(tuple(topic.topic_id for topic in dimension.topics))
            limitations = list(item.limitations)
            for label, hours in (("long-term", 720), ("short-term", 24)):
                if item.reader_count and start - timedelta(hours=hours) < _time(self.manifest["coverage_start"]):
                    limitations.append(f"{label} history is truncated at package coverage start; lookback is a maximum, not complete prior coverage")
            item = item.model_copy(update={"limitations": tuple(limitations)})
            items.append(item)
        snapshot = sha256(_json({"release": self.source_release_sha256, "tenant": tenant,
            "start": _iso(start), "end": _iso(end), "revision": self.manifest["snapshot_revision"]}).encode()).hexdigest()
        return ReaderProfileBatch(tenant_id=query.tenant_id, window_start=start, window_end=end,
            source_release_sha256=self.source_release_sha256, query_fingerprint=reader_profile_query_fingerprint(query),
            snapshot_id=snapshot, source_revision=self.manifest["snapshot_revision"],
            taxonomy_version=self.expected_taxonomy_version, profile_policy_version=self.expected_profile_policy_version,
            generated_at=_time(self.manifest["generated_at"]), expires_at=None,
            watermark=_time(self.manifest["source_watermark"]), is_complete=True, source_mode="synthetic", items=tuple(items))

    async def fetch(self, query: ReaderProfileQuery) -> ReaderProfileBatch:
        if self._closed:
            raise ReaderProfileProviderError("closed")
        if not isinstance(query, ReaderProfileQuery):
            raise ReaderProfileProviderError("invalid_query")
        try:
            result = await asyncio.to_thread(self._fetch_sync, query)
            if self._closed:
                raise ReaderProfileProviderError("closed")
            return result
        except ReaderProfileProviderError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise ReaderProfileProviderError("invalid_response") from exc
        except (OSError, sqlite3.Error) as exc:
            raise ReaderProfileProviderError("unavailable", retryable=True) from exc

    async def close(self):
        self._closed = True
