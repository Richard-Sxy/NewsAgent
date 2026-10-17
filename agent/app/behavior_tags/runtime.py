"""Read-only, version-bound access to explicitly synthetic local behavior packages."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from app.analytics.news_content import NewsContent
from app.behavior_tags.tagging import TopicPolicy, tag_article
from app.retrieval.related_news_reranker import RerankRelatedNews


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("behavior context requires aware timestamps")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class BehaviorRetrievalContext:
    """One immutable aggregate profile snapshot per run; never holds recipient IDs."""

    identity_sha256: str
    package_sha256: str
    tenant_id: str
    cohort_id: str
    profile_as_of: str
    profile_version: str
    policy: TopicPolicy
    topic_weights: tuple[tuple[str, float], ...]
    profile_count: int
    max_bonus: float

    def rerank(self, source: NewsContent, candidates: list[RerankRelatedNews], *,
               limit: int) -> tuple[list[RerankRelatedNews], dict]:
        if limit < 1:
            raise ValueError("related limit must be positive")
        source_tags = tag_article({"tenant_id": self.tenant_id, "news_id": source.news_id,
            "title": source.title, "content": source.body or source.summary or source.title}, self.policy)
        source_topics = set(source_tags["topic_ids"])
        weights = dict(self.topic_weights)
        scored = []
        details = []
        for original in candidates:
            candidate = original.news
            tags = tag_article({"tenant_id": self.tenant_id, "news_id": candidate.news_id or "unbound",
                "title": candidate.title or candidate.text[:200] or "未命名",
                "content": candidate.text or candidate.title or "暂无内容"}, self.policy)
            shared = sorted(source_topics & set(tags["topic_ids"]))
            # A matching interest must not cancel an existing entity-conflict penalty.
            conflict = any("不一致" in reason or "缺少相同核心主体" in reason for reason in original.reasons)
            affinity = max((weights.get(topic, 0.0) for topic in shared), default=0.0)
            bonus = 0.0 if conflict else round(self.max_bonus * affinity, 8)
            final = round(original.final_score + bonus, 8)
            reasons = original.reasons + ((f"合成兴趣主题加分 {bonus:.4f}（规则 {self.policy.policy_version}）",)
                if bonus > 0 else ())
            scored.append(replace(original, final_score=final, reasons=reasons))
            details.append({"news_id": candidate.news_id, "shared_topic_ids": shared,
                "base_score": original.final_score, "interest_bonus": bonus,
                "final_score": final, "entity_conflict": conflict})
        # Stable sort preserves the original order on ties.
        scored.sort(key=lambda item: item.final_score, reverse=True)
        trace = {"source_mode": "synthetic", "context_sha256": self.identity_sha256,
            "package_sha256": self.package_sha256, "taxonomy_version": self.policy.catalog.taxonomy_version,
            "policy_version": self.policy.policy_version, "profile_version": self.profile_version,
            "profile_as_of": self.profile_as_of, "cohort_id": self.cohort_id,
            "aggregate_profile_count": self.profile_count, "max_bonus": self.max_bonus,
            "source_topic_ids": sorted(source_topics), "candidates": details,
            "before_news_ids": [item.news.news_id for item in candidates],
            "after_news_ids": [item.news.news_id for item in scored[:limit]],
            "hot_score_changed": False}
        return scored[:limit], trace


class LocalBehaviorRuntime:
    def __init__(self, package_path: str | Path, *, environment: str,
                 cohort_id: str = "all", max_bonus: float = 0.08) -> None:
        if environment not in {"local", "development", "test", "e2e"}:
            raise ValueError("synthetic behavior packages are restricted to local environments")
        if not math.isfinite(max_bonus) or not 0 <= max_bonus <= 0.08:
            raise ValueError("behavior topic bonus must be between zero and .08")
        if not cohort_id or len(cohort_id) > 80:
            raise ValueError("invalid behavior cohort")
        self.directory = Path(package_path).resolve(strict=True)
        manifest_path = self.directory / "manifest.json"
        if manifest_path.is_symlink():
            raise ValueError("behavior manifest must be a regular file")
        with manifest_path.open("rb") as stream:
            payload = stream.read(1_048_577)
        if len(payload) > 1_048_576:
            raise ValueError("behavior manifest exceeds limit")
        self.manifest = json.loads(payload)
        if type(self.manifest.get("schema_version")) is not int or self.manifest["schema_version"] != 1:
            raise ValueError("unsupported behavior package schema")
        identity = dict(self.manifest)
        declared_identity = identity.pop("identity_sha256", None)
        if declared_identity != hashlib.sha256(_json(identity).encode()).hexdigest():
            raise ValueError("behavior manifest identity mismatch")
        self.package_sha256 = hashlib.sha256(payload).hexdigest()
        artifacts = self.manifest.get("artifacts", {})
        if not {"policy.json", "label_index.sqlite"} <= set(artifacts):
            raise ValueError("behavior package is incomplete")
        total_bytes = 0
        for name, metadata in artifacts.items():
            if Path(name).name != name or name in {".", "..", "manifest.json"}:
                raise ValueError("invalid behavior artifact path")
            artifact = self.directory / name
            if artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_size > 4_294_967_296:
                raise ValueError("invalid behavior artifact")
            total_bytes += artifact.stat().st_size
            if total_bytes > 8_589_934_592:
                raise ValueError("behavior package exceeds local size bound")
            digest = hashlib.sha256()
            with artifact.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != metadata["sha256"]:
                raise ValueError("behavior artifact digest mismatch")
        self.policy = TopicPolicy.model_validate_json((self.directory / "policy.json").read_bytes())
        if self.policy.catalog.taxonomy_version != self.manifest["taxonomy_version"]:
            raise ValueError("behavior taxonomy version mismatch")
        if self.policy.policy_version != self.manifest["policy_version"]:
            raise ValueError("behavior policy version mismatch")
        self.cohort_id = cohort_id
        self.max_bonus = max_bonus

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect((self.directory / "label_index.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        return connection

    def profiles_at(self, *, tenant_id: str, window_start: datetime,
                    window_end: datetime) -> tuple[str, list[dict]]:
        start, end = _time(window_start), _time(window_end)
        window = self.manifest["source_window"]
        if start >= end or start < _time(window["window_start"]) or end > _time(window["window_end"]):
            raise ValueError("behavior package does not cover the requested window")
        with self._connect() as connection:
            timestamps = [row[0] for row in connection.execute(
                "SELECT DISTINCT as_of FROM profiles WHERE tenant_id=?", (tenant_id,))]
            eligible = [stamp for stamp in timestamps if _time(stamp) <= end]
            if not eligible:
                raise ValueError("no non-future behavior snapshot for this tenant/window")
            chosen = max(eligible, key=_time)
            if end - _time(chosen) > timedelta(hours=48):
                raise ValueError("behavior profile snapshot is stale for this window")
            profiles = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM profiles WHERE tenant_id=? AND as_of=? ORDER BY synthetic_user_id",
                (tenant_id, chosen))]
        if len(profiles) > 10000:
            raise ValueError("behavior profile population exceeds local bound")
        if any(item["tenant_id"] != tenant_id or item["as_of"] != chosen for item in profiles):
            raise ValueError("behavior profile identity mismatch")
        if any(item.get("profile_version") != self.manifest["profile_version"] for item in profiles):
            raise ValueError("behavior profile version mismatch")
        if self.cohort_id != "all":
            profiles = [item for item in profiles if item["cohort_id"] == self.cohort_id]
        if not profiles:
            raise ValueError("requested behavior cohort has no profiles")
        return chosen, profiles

    def context_for_window(self, *, tenant_id: str, window_start: datetime,
                           window_end: datetime) -> BehaviorRetrievalContext:
        as_of, profiles = self.profiles_at(tenant_id=tenant_id, window_start=window_start, window_end=window_end)
        sums: dict[str, float] = {}
        for profile in profiles:
            for topic, weight in profile["topic_weights"].items():
                self.policy.catalog.validate_ids((topic,))
                if not isinstance(weight, (int, float)) or not math.isfinite(weight) or not 0 <= weight <= 1:
                    raise ValueError("invalid behavior interest weight")
                sums[topic] = sums.get(topic, 0) + weight
        weights = tuple(sorted((topic, round(value / len(profiles), 8)) for topic, value in sums.items()))
        identity = {"package_sha256": self.package_sha256, "tenant_id": tenant_id,
            "cohort_id": self.cohort_id, "profile_as_of": as_of, "max_bonus": self.max_bonus,
            "rerank_version": "local-topic-affinity-v1"}
        return BehaviorRetrievalContext(hashlib.sha256(_json(identity).encode()).hexdigest(),
            self.package_sha256, tenant_id, self.cohort_id, as_of, self.manifest["profile_version"],
            self.policy, weights, len(profiles), self.max_bonus)

    def search(self, *, tenant_id: str, window_start: datetime, window_end: datetime,
               include_topic_ids: tuple[str, ...] = (), exclude_topic_ids: tuple[str, ...] = (),
               match: str = "any", behavior_label_ids: tuple[str, ...] = (), limit: int = 20,
               dataset_version: str | None = None, scenario_id: str | None = None) -> list[dict]:
        self.policy.catalog.validate_ids(include_topic_ids)
        self.policy.catalog.validate_ids(exclude_topic_ids)
        if match not in {"any", "all"} or not 1 <= limit <= 100 or len(behavior_label_ids) > 32:
            raise ValueError("invalid behavior search options")
        start, end = _time(window_start), _time(window_end)
        window = self.manifest["source_window"]
        if start >= end or start < _time(window["window_start"]) or end > _time(window["window_end"]):
            raise ValueError("behavior package does not cover the requested search window")
        with self._connect() as connection:
            content = {row[0]: json.loads(row[1]) for row in connection.execute(
                "SELECT news_id,payload_json FROM content_labels WHERE tenant_id=? ORDER BY news_id", (tenant_id,))}
            results: dict[tuple[str, str, str, str], dict] = {}
            for news_id, a, b, payload in connection.execute(
                "SELECT news_id,window_start,window_end,payload_json FROM behavior_labels "
                "WHERE tenant_id=? AND julianday(window_start)>=julianday(?) "
                "AND julianday(window_end)<=julianday(?) ORDER BY window_end,news_id",
                (tenant_id, start.isoformat(), end.isoformat())):
                if _time(a) < start or _time(b) > end or news_id not in content:
                    continue
                tags = content[news_id]
                topics = set(tags["topic_ids"])
                if topics & set(exclude_topic_ids):
                    continue
                if include_topic_ids and not (set(include_topic_ids) <= topics if match == "all" else topics & set(include_topic_ids)):
                    continue
                behavior = json.loads(payload)
                if dataset_version is not None and behavior.get("source_dataset_version") != dataset_version:
                    continue
                if scenario_id is not None and behavior.get("scenario_id") != scenario_id:
                    continue
                labels = behavior.get("label_ids", ())
                if not set(behavior_label_ids) <= set(labels):
                    continue
                dataset = behavior.get("source_dataset_version", "single")
                results[(news_id, dataset, behavior.get("scenario_id", "single"),
                    behavior.get("metric_definition_id", "single"))] = {"content": tags, "behavior": behavior,
                    "package_sha256": self.package_sha256, "source_mode": "synthetic"}
        return sorted(results.values(), key=lambda item: (item["behavior"].get("window_end", ""),
            item["content"]["news_id"]), reverse=True)[:limit]


def build_behavior_runtime(settings) -> LocalBehaviorRuntime | None:
    if not getattr(settings, "behavior_tags_enabled", False):
        return None
    if not settings.behavior_tags_package_path:
        raise ValueError("BEHAVIOR_TAGS_PACKAGE_PATH is required")
    return LocalBehaviorRuntime(settings.behavior_tags_package_path, environment=settings.environment,
        cohort_id=settings.behavior_tags_cohort_id, max_bonus=settings.behavior_tags_max_bonus)
