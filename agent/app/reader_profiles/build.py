"""Materialize exact, anonymous news-reader aggregates from synthetic events only.

The source package is immutable. No person identifiers or events are copied to
the published package; its SQLite tables contain content bindings, coverage,
and one aggregate row per actually visited news/window.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import sqlite3
import tempfile

from app.behavior_tags.build import GENERATOR_VERSION, PROFILE_HALF_LIFE_SECONDS, _event_score
from app.behavior_tags.tagging import TopicPolicy
from app.reader_profiles.contracts import (MIN_LIFT_SUPPORT, PROFILE_POLICY_VERSION,
    ReaderProfileSummary, ratio_decimal)

PACKAGE_VERSION = "news-reader-profiles-local-v1"
POLICY_VERSION = PROFILE_POLICY_VERSION
METRIC_POLICY_VERSION = "synthetic-detail-view-distinct-readers-v1"
MAX_MANIFEST_BYTES = 2_097_152
MAX_POLICY_BYTES = 1_048_576
MAX_LINE_BYTES = 2_097_152
MAX_EVENTS = 1_000_000
MAX_CONTENT = 20_000
MAX_USERS = 5_000
MAX_WINDOWS = 744
MAX_ARTIFACT_BYTES = 1_073_741_824
ALLOWED_ENVIRONMENTS = {"local", "development", "test", "e2e"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _loads(raw):
    return json.loads(raw, object_pairs_hook=_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON value")))


def _time(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("reader profiles require aware timestamps")
    return stamp.astimezone(UTC)


def _iso(value):
    return _time(value).isoformat()


def _hash_file(path):
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular(path, maximum):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ValueError("missing, nonregular or oversized reader-profile input")


def _manifest(path):
    _regular(path, MAX_MANIFEST_BYTES)
    raw = path.read_bytes()
    data = _loads(raw)
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported reader-profile source schema")
    identity = dict(data)
    declared = identity.pop("identity_sha256", None)
    if declared != sha256(_json(identity).encode()).hexdigest():
        raise ValueError("manifest identity mismatch")
    return data, sha256(raw).hexdigest()


def _rows(directory, manifest, name, maximum):
    spec = manifest.get("artifacts", {}).get(name)
    if not isinstance(spec, dict) or type(spec.get("rows")) is not int or not 0 <= spec["rows"] <= maximum:
        raise ValueError("invalid source row-count declaration")
    path = directory / name
    if spec["rows"] == 0 and path.is_file() and not path.is_symlink() and path.stat().st_size == 0:
        if spec.get("sha256") != sha256(b"").hexdigest() or spec.get("bytes") != 0:
            raise ValueError("empty input hash mismatch")
        return
    _regular(path, MAX_ARTIFACT_BYTES)
    if path.stat().st_size != spec.get("bytes"):
        raise ValueError("input size mismatch")
    digest, count = sha256(), 0
    with path.open("rb") as stream:
        while raw := stream.readline(MAX_LINE_BYTES + 1):
            if len(raw) > MAX_LINE_BYTES:
                raise ValueError("input JSONL line exceeds bound")
            digest.update(raw)
            count += 1
            if count > maximum:
                raise ValueError("input row budget exceeded")
            row = _loads(raw)
            if not isinstance(row, dict):
                raise ValueError("input JSONL row must be an object")
            yield row
    if count != spec["rows"] or digest.hexdigest() != spec.get("sha256"):
        raise ValueError("input row count or SHA-256 mismatch")


def _read_source(directory):
    directory = Path(directory).resolve(strict=True)
    manifest, manifest_hash = _manifest(directory / "manifest.json")
    if manifest.get("package_version") != "behavior-tags-local-v1" or manifest.get("generator_version") != GENERATOR_VERSION:
        raise ValueError("reader profiles accept only supported synthetic behavior packages")
    tenants = manifest.get("tenants")
    if not isinstance(tenants, list) or not tenants or len(tenants) != len(set(tenants)) or any(not isinstance(t, str) or not t for t in tenants):
        raise ValueError("invalid source tenants")
    start = _time(manifest["simulation_window"]["window_start"])
    end = _time(manifest["simulation_window"]["window_end"])
    if start >= end or end - start > timedelta(days=31) or _time(manifest["build_as_of"]) < end:
        raise ValueError("invalid synthetic event coverage")
    policy_path = directory / "policy.json"
    _regular(policy_path, MAX_POLICY_BYTES)
    spec = manifest.get("artifacts", {}).get("policy.json", {})
    if spec.get("sha256") != _hash_file(policy_path) or spec.get("bytes") != policy_path.stat().st_size:
        raise ValueError("source policy hash/size mismatch")
    topic_policy = TopicPolicy.model_validate(_loads(policy_path.read_bytes()))
    if topic_policy.policy_version != manifest.get("policy_version") or topic_policy.catalog.taxonomy_version != manifest.get("taxonomy_version"):
        raise ValueError("source topic policy version mismatch")
    contents = {}
    for row in _rows(directory, manifest, "content_labels.jsonl", MAX_CONTENT):
        key = row.get("tenant_id"), row.get("news_id")
        if key[0] not in tenants or not isinstance(key[1], str) or not key[1] or key in contents:
            raise ValueError("duplicate or invalid content identity")
        topic_policy.catalog.validate_ids(row.get("topic_ids", ()))
        digest = row.get("content_sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid source content hash")
        contents[key] = {"content_sha256": digest, "topic_ids": tuple(row["topic_ids"])}
    events = defaultdict(list)
    seen, people, initialized = set(), set(), {}
    kinds = {"profile_initialized", "impression", "click", "detail_view", "interaction", "negative_feedback"}
    for row in _rows(directory, manifest, "synthetic_events.jsonl", MAX_EVENTS):
        tenant, user, event_id = row.get("tenant_id"), row.get("synthetic_user_id"), row.get("event_id")
        kind = row.get("event_type")
        stamp = _time(row.get("occurred_at"))
        if (tenant not in tenants or not isinstance(user, str) or not user.startswith("synthetic-")
                or not isinstance(event_id, str) or not event_id.startswith("synthetic-event-")
                or event_id in seen or kind not in kinds or not start <= stamp < end
                or row.get("source_mode") != "synthetic" or row.get("generator_version") != GENERATOR_VERSION
                or row.get("not_reconstructed_from_aggregate_uv") is not True):
            raise ValueError("event identity/time/synthetic provenance mismatch")
        seen.add(event_id)
        people.add((tenant, user))
        if len(people) > MAX_USERS:
            raise ValueError("synthetic population exceeds bound")
        if kind == "profile_initialized":
            if (tenant, user) in initialized or row.get("news_id") is not None or row.get("topic_ids") != []:
                raise ValueError("duplicate or invalid profile initialization")
            initialized[(tenant, user)] = stamp
        else:
            key = tenant, row.get("news_id")
            content = contents.get(key)
            if content is None or row.get("content_sha256") != content["content_sha256"] or tuple(row.get("topic_ids", ())) != content["topic_ids"]:
                raise ValueError("event content/topic binding mismatch")
        if type(row.get("duration_seconds")) is not int or not 0 <= row["duration_seconds"] <= 86_400 or type(row.get("effective_read")) is not bool:
            raise ValueError("invalid synthetic duration/effective-read fields")
        score = _event_score(row)
        events[tenant].append((stamp, user, row.get("news_id"), kind, tuple(row.get("topic_ids", ())), score))
    for rows in events.values():
        rows.sort(key=lambda item: (item[0], item[1], item[2] or "", item[3]))
    return manifest, manifest_hash, topic_policy, contents, events, initialized, start, end


class _RollingState:
    """One pass per horizon; common exponential factor cancels in normalization."""
    def __init__(self, events, hours, origin):
        self.events, self.hours, self.origin = events, hours, origin
        self.cursor = self.expired = 0
        self.scores = defaultdict(dict)
        self.news_scores = defaultdict(dict)
        self.evidence = Counter()
        self.news_evidence = defaultdict(Counter)

    def _apply(self, event, sign):
        stamp, user, news, kind, topics, score = event
        if not score:
            return
        coefficient = sign * score * 2 ** ((stamp - self.origin).total_seconds() / PROFILE_HALF_LIFE_SECONDS)
        personal = self.scores[user]
        contribution = self.news_scores[news].setdefault(user, {})
        for topic in topics:
            personal[topic] = personal.get(topic, 0) + coefficient
            contribution[topic] = contribution.get(topic, 0) + coefficient
            if abs(personal[topic]) < 1e-10:
                personal.pop(topic, None)
            if abs(contribution[topic]) < 1e-10:
                contribution.pop(topic, None)
        self.evidence[user] += sign
        self.news_evidence[news][user] += sign
        if self.news_evidence[news][user] == 0:
            del self.news_evidence[news][user]
            self.news_scores[news].pop(user, None)

    def advance(self, boundary):
        while self.cursor < len(self.events) and self.events[self.cursor][0] < boundary:
            self._apply(self.events[self.cursor], 1)
            self.cursor += 1
        lower = boundary - timedelta(hours=self.hours)
        while self.expired < self.cursor and self.events[self.expired][0] < lower:
            self._apply(self.events[self.expired], -1)
            self.expired += 1

    def profile(self, user, initialized, news=None):
        if not initialized:
            return frozenset(), False, False
        excluded = self.news_scores.get(news, {}).get(user, {}) if news else {}
        positive = {topic: score - excluded.get(topic, 0) for topic, score in self.scores.get(user, {}).items()
            if score - excluded.get(topic, 0) > 1e-9}
        # Topics which cancel to zero in the full profile can become positive
        # when a negative contribution from the trigger news is excluded.
        for topic, value in excluded.items():
            remaining = self.scores.get(user, {}).get(topic, 0) - value
            if remaining > 1e-9:
                positive[topic] = remaining
        evidence = self.evidence[user] - (self.news_evidence.get(news, {}).get(user, 0) if news else 0)
        if not positive:
            return frozenset(), False, evidence == 0
        maximum = max(positive.values())
        return frozenset(topic for topic, score in positive.items() if score / maximum >= .2 - 1e-12), True, False


def _ratio(numerator, denominator):
    return ratio_decimal(numerator, denominator)


def _dimension(state, users, visitors, known, base, news, *, top_k, minimum_sample, names):
    baseline_topics, baseline_profiled = base[0].copy(), base[1]
    affected = set(state.news_scores.get(news, {})) | set(state.news_evidence.get(news, {}))
    # Only users with trigger-news history need baseline correction.
    for user in affected:
        old = base[2].get(user, (frozenset(), False, False))
        current = state.profile(user, known.get(user, False), news)
        baseline_profiled += int(current[1]) - int(old[1])
        baseline_topics.subtract(old[0])
        baseline_topics.update(current[0])
    topic_counts, profiled, cold = Counter(), 0, 0
    for user in visitors:
        topics, available, is_cold = state.profile(user, known.get(user, False), news)
        profiled += int(available)
        cold += int(is_cold)
        topic_counts.update(topics)
    total = len(visitors)
    items = []
    for topic, count in sorted(topic_counts.items(), key=lambda item: (-item[1], item[0]))[:top_k]:
        baseline_count = baseline_topics[topic]
        share_profiled = _ratio(count, profiled)
        baseline_share = _ratio(baseline_count, baseline_profiled)
        low_sample = min(profiled, baseline_profiled, count, baseline_count) < minimum_sample
        items.append(dict(topic_id=topic, reader_count=count,
            share_all=_ratio(count, total), share_profiled=share_profiled,
            baseline_reader_count=baseline_count, baseline_profiled_count=baseline_profiled,
            baseline_share=baseline_share,
            lift=(share_profiled / baseline_share).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)
                if not low_sample and baseline_share else None,
            low_sample=low_sample))
    return dict(lookback_hours=state.hours, profiled_reader_count=profiled,
        unknown_reader_count=total - profiled, cold_start_reader_count=cold,
        baseline_profiled_count=baseline_profiled, topics=items)


def _create_database(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA page_size=4096;
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE contents(tenant_id TEXT NOT NULL,news_id TEXT NOT NULL,payload_json TEXT NOT NULL,
            PRIMARY KEY(tenant_id,news_id));
        CREATE TABLE coverage(tenant_id TEXT NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL,
            payload_json TEXT NOT NULL,PRIMARY KEY(tenant_id,window_start,window_end));
        CREATE TABLE reader_profiles(tenant_id TEXT NOT NULL,news_id TEXT NOT NULL,window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,payload_json TEXT NOT NULL,PRIMARY KEY(tenant_id,news_id,window_start,window_end));
    """)
    return connection


def build_package(behavior_package, output_dir, *, window_hours=1, top_k=20, minimum_sample=MIN_LIFT_SUPPORT):
    if type(window_hours) is not int or window_hours not in {1, 24} or type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError("window_hours must be 1 or 24 and top_k must be between 1 and 20")
    if type(minimum_sample) is not int or not MIN_LIFT_SUPPORT <= minimum_sample <= 10_000:
        raise ValueError("invalid lift minimum sample size")
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output directory already exists")
    manifest, source_hash, topic_policy, contents, events, initialized, start, end = _read_source(behavior_package)
    # Midnight anchoring is unnecessary: every requested interval is exactly a
    # configured number of whole hours and lies inside declared simulation data.
    boundary = start.replace(minute=0, second=0, microsecond=0)
    if boundary < start:
        boundary += timedelta(hours=1)
    windows = []
    while boundary + timedelta(hours=window_hours) <= end:
        windows.append((boundary, boundary + timedelta(hours=window_hours)))
        boundary += timedelta(hours=window_hours)
    if not windows or len(windows) > MAX_WINDOWS:
        raise ValueError("no bounded complete windows in source")
    policy = dict(schema_version=1, policy_version=POLICY_VERSION, metric_policy_version=METRIC_POLICY_VERSION,
        taxonomy_version=topic_policy.catalog.taxonomy_version, source_mode="synthetic",
        catalog=topic_policy.catalog.model_dump(mode="json"), source_topic_policy_version=topic_policy.policy_version,
        reader_event_type="detail_view", count_method="exact_distinct_synthetic_user_per_tenant_news_window",
        long_term_hours=720, short_term_hours=24, half_life_seconds=PROFILE_HALF_LIFE_SECONDS,
        membership_min_normalized_weight=.2, minimum_lift_sample=minimum_sample,
        profile_available="initialization visible at window_start and positive interest excluding trigger news",
        cold_start="initialized but no scored non-trigger events in the horizon; subset of unknown",
        history_interval="[window_start-horizon,window_start)", trigger_news_excluded=True,
        baseline="same tenant, same window_start; every synthetic initialized profile; trigger news excluded",
        top_k=top_k, window_hours=window_hours, event_score_version="behavior-tags-event-score-v1",
        event_score_source_sha256=_hash_file(Path(__file__).parents[1] / "behavior_tags/build.py"),
        news_topics_are_reader_interests=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
    connection = None
    try:
        connection = _create_database(work / "reader_profiles.sqlite")
        (work / "policy.json").write_text(_json(policy) + "\n", encoding="utf-8")
        names = {topic.topic_id: topic.name for topic in topic_policy.catalog.topics}
        counts = Counter(content_bindings=len(contents), reader_profiles=0, covered_windows=0)
        for (tenant, news), binding in sorted(contents.items()):
            connection.execute("INSERT INTO contents VALUES(?,?,?)", (tenant, news, _json(binding)))
        for tenant in manifest["tenants"]:
            rows = events[tenant]
            users = sorted({row[1] for row in rows})
            states = [_RollingState(rows, hours, start) for hours in (720, 24)]
            cursor = 0
            for window_start, window_end in windows:
                known = {user: initialized.get((tenant, user), end) <= window_start for user in users}
                bases = []
                for state in states:
                    state.advance(window_start)
                    profiles = {user: state.profile(user, known[user]) for user in users}
                    topics = Counter()
                    for value in profiles.values():
                        topics.update(value[0])
                    bases.append((topics, sum(int(value[1]) for value in profiles.values()), profiles))
                readers = defaultdict(set)
                while cursor < len(rows) and rows[cursor][0] < window_end:
                    stamp, user, news, kind, _, _ = rows[cursor]
                    if stamp >= window_start and kind == "detail_view":
                        readers[news].add(user)
                    cursor += 1
                coverage = dict(source_mode="synthetic", window_start=_iso(window_start), window_end=_iso(window_end),
                    complete=True, population_count=len(users), initialized_profile_count=sum(known.values()))
                connection.execute("INSERT INTO coverage VALUES(?,?,?,?)", (tenant, _iso(window_start), _iso(window_end), _json(coverage)))
                counts["covered_windows"] += 1
                for news, visitors in sorted(readers.items()):
                    dimensions = [_dimension(state, users, visitors, known, base, news,
                        top_k=top_k, minimum_sample=minimum_sample, names=names) for state, base in zip(states, bases)]
                    binding = contents[(tenant, news)]
                    payload = ReaderProfileSummary(news_id=news, source_mode="synthetic",
                        taxonomy_version=topic_policy.catalog.taxonomy_version, profile_policy_version=POLICY_VERSION,
                        content_sha256=binding["content_sha256"], content_topic_ids=tuple(binding["topic_ids"]),
                        reader_count=len(visitors), status="complete", long_term=dimensions[0], short_term=dimensions[1],
                        profile_cutoff=window_start, trigger_article_excluded=True,
                        count_method="exact_synthetic_distinct", error_bound=None,
                        limitations=("synthetic readers only; not enterprise audience or delivery eligibility",
                            "interest membership uses normalized positive score >= 0.2; multi-label shares do not sum to one"))
                    connection.execute("INSERT INTO reader_profiles VALUES(?,?,?,?,?)", (tenant, news, _iso(window_start), _iso(window_end), _json(payload.model_dump(mode="json"))))
                    counts["reader_profiles"] += 1
        output_manifest = dict(schema_version=1, package_version=PACKAGE_VERSION, source_mode="synthetic",
            source_behavior_identity_sha256=manifest["identity_sha256"], source_manifest_sha256=source_hash,
            source_artifacts={name: manifest["artifacts"][name] for name in ("policy.json", "content_labels.jsonl", "synthetic_events.jsonl")},
            taxonomy_version=topic_policy.catalog.taxonomy_version, profile_policy_version=POLICY_VERSION,
            metric_policy_version=METRIC_POLICY_VERSION, window_hours=window_hours, top_k=top_k,
            tenants=manifest["tenants"], coverage_start=_iso(windows[0][0]), coverage_end=_iso(windows[-1][1]),
            generated_at=_iso(_time(manifest["build_as_of"])), source_watermark=_iso(end), snapshot_revision=1,
            counts=dict(counts), private_rows_in_output=False)
        connection.execute("INSERT INTO metadata VALUES(?,?)", ("manifest_core_json", _json(output_manifest)))
        connection.commit()
        connection.execute("VACUUM")
        connection.close()
        connection = None
        output_manifest["artifacts"] = {name: {"sha256": _hash_file(work / name), "bytes": (work / name).stat().st_size}
            for name in ("policy.json", "reader_profiles.sqlite")}
        if any(spec["bytes"] > MAX_ARTIFACT_BYTES for spec in output_manifest["artifacts"].values()):
            raise ValueError("reader-profile package exceeds artifact budget")
        output_manifest["identity_sha256"] = sha256(_json(output_manifest).encode()).hexdigest()
        (work / "manifest.json").write_text(_json(output_manifest) + "\n", encoding="utf-8")
        output.mkdir()
        try:
            work.rename(output)
        except OSError:
            output.rmdir()
            raise
        return output_manifest
    finally:
        if connection is not None:
            connection.close()
        if work.exists():
            shutil.rmtree(work)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--behavior-package", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--window-hours", type=int, choices=(1, 24), default=1)
    parser.add_argument("--top-k", type=int, default=20)
    args = parser.parse_args(argv)
    result = build_package(args.behavior_package, args.output_dir, window_hours=args.window_hours, top_k=args.top_k)
    print(_json({"output_dir": str(args.output_dir.absolute()), "identity_sha256": result["identity_sha256"], "counts": result["counts"]}))


if __name__ == "__main__":
    main()
