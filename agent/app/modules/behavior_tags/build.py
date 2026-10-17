"""Build a new, reproducible local label package without changing its warehouse.

Aggregate click counts influence only synthetic sampling probabilities. They do
not identify individuals or constrain the generated events to reconstruct UV.
All profile scores and counts are calculated exclusively from synthetic events.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import random
import shutil
import sqlite3
import tempfile

from app.modules.behavior_tags.tagging import (
    BEHAVIOR_RULE_VERSION, TopicPolicy, behavior_labels, load_topic_policy, tag_article,
)

PACKAGE_VERSION = "behavior-tags-local-v1"
GENERATOR_VERSION = "warehouse-guided-synthetic-events-v1"
PROFILE_VERSION = "synthetic-interest-profile-v1"
COHORT_IDS = ("focused", "cross_topic", "deep_reader", "skim_reader", "explorer",
              "negative_feedback", "interest_shift", "cold_start",
              "notification_disabled", "frequency_limited")
CHANNELS = ("homefeed", "search", "topic_page", "push", "deep_link")
DEVICES = ("desktop", "mobile_web")
MAX_EVENTS = 1_000_000
MAX_ARTICLES = 20_000
MAX_OBSERVATIONS = 1_000_000
MAX_TOTAL_USERS = 5_000
MAX_PROFILE_ROWS = 160_000
DEFAULT_POLICY = Path(__file__).resolve().parents[2] / "deploy" / "behavior-topic-policy.local.yml"
PROFILE_HALF_LIFE_SECONDS = 3 * 86400


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("label package times must be timezone aware")
    return result.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _time(value).isoformat()


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_source(source_dir):
    from app.modules.behavior_tags.merge import load_merged_source
    return load_merged_source([source_dir])


def _read_merged_sources(source_dirs):
    from app.modules.behavior_tags.merge import load_merged_source
    return load_merged_source(source_dirs)


def _snapshot_times(start, end):
    times = [start]
    boundary = datetime.combine(start.date() + timedelta(days=1), datetime.min.time(), UTC)
    while boundary < end:
        times.append(boundary)
        boundary += timedelta(days=1)
    if end != start:
        times.append(end)
    return times


def _event_score(event):
    kind = event["event_type"]
    if kind == "click":
        return 1.0
    if kind == "detail_view":
        return min(event["duration_seconds"] / 60, 3) + (1 if event["effective_read"] else 0)
    if kind == "interaction":
        return 2.0
    if kind == "negative_feedback":
        return -3.0
    return 0.0


def profile_from_events(events: list[dict], *, as_of: datetime, root_ids: set[str]) -> dict:
    """Deterministic daily profile: use only events visible at this snapshot time."""
    visible = [event for event in events if _time(event["occurred_at"]) <= as_of]
    initialized = next((event for event in visible if event["event_type"] == "profile_initialized"), None)
    if initialized is None:
        raise ValueError("synthetic profile has no visible initialization event")
    counts = dict(impressions=0, clicks=0, detail_views=0, effective_reads=0,
                  interactions=0, negative_feedback=0, total_duration_seconds=0)
    fields = {"impression": "impressions", "click": "clicks", "detail_view": "detail_views",
              "interaction": "interactions", "negative_feedback": "negative_feedback"}
    scores, recent, older = defaultdict(float), defaultdict(float), defaultdict(float)
    channels, hours, devices = Counter(), Counter(), Counter()
    for event in visible:
        kind = event["event_type"]
        if kind in fields:
            counts[fields[kind]] += 1
        if kind == "impression":
            channels[event["channel"]] += 1
            devices[event["device"]] += 1
            hours[str((_time(event["occurred_at"]) + timedelta(hours=8)).hour)] += 1
        if kind == "detail_view":
            counts["effective_reads"] += int(event["effective_read"])
            counts["total_duration_seconds"] += event["duration_seconds"]
        age = (as_of - _time(event["occurred_at"])).total_seconds()
        score = _event_score(event)
        for topic in event["topic_ids"]:
            scores[topic] += score * 0.5 ** (age / PROFILE_HALF_LIFE_SECONDS)
            if score > 0 and topic in root_ids:
                (recent if age <= 3 * 86400 else older)[topic] += score
    positive = {topic: score for topic, score in scores.items() if score > 0}
    maximum = max(positive.values(), default=1)
    weights = {topic: round(score / maximum, 8) for topic, score in sorted(positive.items())}
    labels = []
    if counts["clicks"] == 0:
        labels.append("profile.cold_start")
    active_roots = [topic for topic in root_ids if weights.get(topic, 0) >= 0.2]
    if active_roots:
        labels.append("profile.cross_topic" if len(active_roots) > 1 else "profile.focused")
    if counts["detail_views"]:
        mean = counts["total_duration_seconds"] / counts["detail_views"]
        if mean >= 90:
            labels.append("profile.deep_reading")
        elif mean <= 20:
            labels.append("profile.shallow_reading")
    if counts["interactions"] >= 2:
        labels.append("profile.interaction_active")
    if counts["negative_feedback"]:
        labels.append("profile.negative_feedback")
    if recent and older and max(recent, key=lambda key: (recent[key], key)) != max(older, key=lambda key: (older[key], key)):
        labels.append("profile.interest_shift")
    bands = (("夜间（00–05点，北京时间）", range(0, 6)), ("上午（06–11点，北京时间）", range(6, 12)),
             ("下午（12–17点，北京时间）", range(12, 18)), ("晚间（18–23点，北京时间）", range(18, 24)))
    active_band = max(bands, key=lambda band: sum(hours[str(hour)] for hour in band[1]))[0] if hours else "无行为（冷启动）"
    return {"tenant_id": initialized["tenant_id"], "synthetic_user_id": initialized["synthetic_user_id"],
        "as_of": _iso(as_of), "cohort_id": initialized["cohort_id"], "topic_weights": weights,
        "behavior_label_ids": sorted(labels), "counts": counts,
        "notification_allowed": initialized["notification_allowed"],
        "frequency_capped": initialized["frequency_capped"], "source_mode": "synthetic",
        "channel_counts": dict(sorted(channels.items())), "hour_counts": dict(sorted(hours.items(), key=lambda item: int(item[0]))),
        "device_counts": dict(sorted(devices.items())), "active_hour_band": active_band,
        "distribution_count_scope": "synthetic impression events only; hours use Asia/Shanghai",
        "profile_version": PROFILE_VERSION, "profile_policy": "event-only; exponential decay; 3-day half-life",
        "notification_policy": "synthetic illustration only; not enterprise eligibility or delivery",
        "events_through": max(event["occurred_at"] for event in visible)}


def _simulate_tenant(tenant, articles, labels, popularity, *, users, seed, start, end, roots):
    """Generate bounded synthetic encounters, retaining real content provenance separately."""
    events, grouped = [], {}
    article_by_id = {article["news_id"]: article for article in articles
                     if article.get("status", "published") == "published"}
    candidates = sorted(article_by_id)
    # Materialize bounded day-prefix distributions once, before generating users.
    # Every reference is complete before day_start, so even the earliest event
    # in that day cannot borrow the next hour/day's observed popularity.
    daily = {}
    source_rows = sorted(popularity, key=lambda row: (_time(row["window_end"]),
        row["source_dataset_version"], row["scenario_id"], row["metric_definition_id"], row["news_id"]))
    cursor, day = 0, start
    accumulated, references = defaultdict(Counter), {}
    while day < end:
        while cursor < len(source_rows) and _time(source_rows[cursor]["window_end"]) <= day:
            row = source_rows[cursor]
            scenario = (row["source_dataset_version"], row["scenario_id"], row["metric_definition_id"])
            accumulated[scenario][row["news_id"]] += row["clicks"]
            references[(scenario, row["news_id"])] = {key: row[key] for key in (
                "source_dataset_version", "scenario_id", "metric_definition_id", "window_start", "window_end")}
            references[(scenario, row["news_id"])]["use"] = "past-only synthetic sampling reference"
            cursor += 1
        daily[_iso(day)] = {"weights": {key: dict(value) for key, value in accumulated.items()},
                            "refs": dict(references)}
        day = min(end, datetime.combine(day.date() + timedelta(days=1), datetime.min.time(), UTC))
    for index in range(users):
        user = f"synthetic-{tenant.replace('-', '')[:12]}-{index:05d}"
        cohort = COHORT_IDS[index % len(COHORT_IDS)]
        rng = random.Random(int(sha256(f"{seed}|{tenant}|{index}".encode()).hexdigest(), 16))
        user_events = []
        current_origin = None
        current_channel = current_device = None

        def emit(kind, when, news_id=None, **values):
            item = {"tenant_id": tenant, "synthetic_user_id": user,
                "event_id": "synthetic-event-" + sha256(f"{seed}|{tenant}|{index}|{len(user_events)}".encode()).hexdigest(),
                "occurred_at": _iso(when), "event_type": kind, "news_id": news_id,
                "topic_ids": labels[news_id]["topic_ids"] if news_id else [],
                "content_sha256": labels[news_id]["content_sha256"] if news_id else None,
                "duration_seconds": 0, "effective_read": False,
                "source_mode": "synthetic", "generator_version": GENERATOR_VERSION,
                "channel": current_channel, "device": current_device,
                "source_dataset_version": current_origin["source_dataset_version"] if current_origin else "unconditioned_synthetic_prior",
                "scenario_id": current_origin["scenario_id"] if current_origin else "synthetic-only",
                "metric_definition_id": current_origin["metric_definition_id"] if current_origin else None,
                "sampling_reference": current_origin,
                "sampling_mode": "past_source_distribution" if current_origin else "unconditioned_synthetic_prior",
                "not_reconstructed_from_aggregate_uv": True, **values}
            user_events.append(item)
            if len(events) + len(user_events) > MAX_EVENTS:
                raise ValueError("synthetic event limit exceeded")

        emit("profile_initialized", start, cohort_id=cohort,
             notification_allowed=cohort != "notification_disabled" and index % 7 != 0,
             frequency_capped=cohort == "frequency_limited")
        primary = roots[index % len(roots)] if roots else None
        secondary = roots[(index + 1) % len(roots)] if roots else None
        day_start = start
        while day_start < end and cohort != "cold_start":
            past = daily[_iso(day_start)]
            day_end = min(end, datetime.combine(day_start.date() + timedelta(days=1), datetime.min.time(), UTC))
            available = [news_id for news_id in candidates
                         if not article_by_id[news_id].get("publish_time")
                         or _time(article_by_id[news_id]["publish_time"]) < day_end - timedelta(minutes=5)]
            if available and (day_end - day_start).total_seconds() > 600:
                favorite = secondary if cohort == "interest_shift" and day_start >= start + (end - start) / 2 else primary
                weights = []
                for news_id in available:
                    topics = labels[news_id]["topic_ids"]
                    preference = 7 if favorite in topics else 1
                    if cohort == "cross_topic" and secondary in topics:
                        preference = 5
                    if cohort in {"explorer", "negative_feedback"}:
                        preference = 1
                    scenario_counts = [counts[news_id] for counts in past["weights"].values() if news_id in counts]
                    # Mix sampling distributions only. No original metric total
                    # is formed across distinct versions or simulated worlds.
                    strength = sum(math.log1p(count) for count in scenario_counts) / len(scenario_counts) if scenario_counts else 0
                    weights.append(preference * (1 + strength))
                for _ in range(rng.randint(2, 6)):
                    news_id = rng.choices(available, weights=weights, k=1)[0]
                    origins = [reference for (scenario, reference_news), reference in past["refs"].items()
                               if reference_news == news_id]
                    current_origin = rng.choice(origins) if origins else None
                    current_channel = rng.choice(CHANNELS)
                    current_device = rng.choices(DEVICES, weights=(1, 3), k=1)[0]
                    published = article_by_id[news_id].get("publish_time")
                    earliest = max(day_start, _time(published)) if published else day_start
                    slack = int((day_end - earliest).total_seconds()) - 300
                    if slack <= 0:
                        continue
                    when = datetime.combine(day_start.date(), datetime.min.time(), UTC) + timedelta(
                        hours=(2 + index % 4 * 6) % 24, seconds=rng.randrange(3000))
                    if not earliest <= when < day_end - timedelta(minutes=5):
                        when = earliest + timedelta(seconds=rng.randrange(slack))
                    emit("impression", when, news_id)
                    probability = 0.8 if cohort == "deep_reader" else 0.3 if cohort == "skim_reader" else 0.55
                    if rng.random() < probability:
                        emit("click", when + timedelta(seconds=1), news_id)
                        duration = (rng.randint(90, 240) if cohort == "deep_reader" else
                                    rng.randint(3, 15) if cohort == "skim_reader" else rng.randint(10, 120))
                        emit("detail_view", when + timedelta(seconds=2), news_id,
                             duration_seconds=duration, effective_read=duration >= 30)
                        if rng.random() < (0.4 if cohort == "deep_reader" else 0.12):
                            emit("interaction", when + timedelta(seconds=duration + 3), news_id,
                                 interaction_type=rng.choice(("like", "comment", "share", "favorite")))
                        if rng.random() < (0.3 if cohort == "negative_feedback" else 0.015):
                            emit("negative_feedback", when + timedelta(seconds=duration + 4), news_id,
                                 feedback_type="not_interested")
            day_start = day_end
        user_events.sort(key=lambda event: (event["occurred_at"], event["event_id"]))
        grouped[user] = user_events
        events.extend(user_events)
    events.sort(key=lambda event: (event["occurred_at"], event["synthetic_user_id"], event["event_id"]))
    return events, grouped


def _create_index(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA page_size=4096;
        CREATE TABLE content_labels(tenant_id TEXT NOT NULL,news_id TEXT NOT NULL,payload_json TEXT NOT NULL,
            PRIMARY KEY(tenant_id,news_id));
        CREATE TABLE behavior_labels(tenant_id TEXT NOT NULL,news_id TEXT NOT NULL,window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,source_dataset_version TEXT NOT NULL,scenario_id TEXT NOT NULL,
            metric_definition_id TEXT NOT NULL,payload_json TEXT NOT NULL,
            PRIMARY KEY(tenant_id,news_id,window_start,window_end,source_dataset_version,scenario_id,metric_definition_id));
        CREATE INDEX behavior_by_window ON behavior_labels(tenant_id,window_start,window_end);
        CREATE TABLE profiles(tenant_id TEXT NOT NULL,synthetic_user_id TEXT NOT NULL,as_of TEXT NOT NULL,
            payload_json TEXT NOT NULL,PRIMARY KEY(tenant_id,synthetic_user_id,as_of));
        CREATE INDEX profiles_by_snapshot ON profiles(tenant_id,as_of);
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    """)
    return connection

"""验证冻结的源并自动发布全新的本地包。"""
def build_package(source_dir: str | Path | list[str | Path] | tuple[str | Path, ...], output_dir: str | Path, *, policy_path: str | Path | None = None,
                  users_per_tenant: int = 600, seed: int = 20261016, days: int = 14) -> dict:
    # 校验参数、读取数据、确定时间范围、限制数据规模
    if type(users_per_tenant) is not int or not 1 <= users_per_tenant <= 2000:
        raise ValueError("users_per_tenant must be within 1 and 2000")
    if type(seed) is not int or not 0 <= seed < 2 ** 63:     # 种子
        raise ValueError("seed must be a nonnegative 63-bit integer")
    if type(days) is not int or not 1 <= days <= 31:         # 时间点
        raise ValueError("simulation days must be within 1 and 31")
    raw_sources = list(source_dir) if isinstance(source_dir, (list, tuple)) else [source_dir]
    if not 1 <= len(raw_sources) <= 16:
        raise ValueError("label builds require between one and sixteen sources")
    source_paths = [str(item) if str(item).startswith("profile:") else Path(item).resolve() for item in raw_sources]
    # 创建临时目录、SQLite、策略文件和计数器
    output = Path(output_dir).resolve()
    if output.exists() or any(isinstance(path, Path) and output.is_relative_to(path) for path in source_paths):
        raise ValueError("output must be new and outside the frozen source directory")
    policy = load_topic_policy(policy_path or DEFAULT_POLICY)
    source = (_read_source(source_paths[0]) if len(source_paths) == 1 and isinstance(source_paths[0], Path)
              else _read_merged_sources(source_paths))
    if not 1 <= len(source.articles) <= MAX_ARTICLES or len(source.observations) > MAX_OBSERVATIONS:
        raise ValueError("warehouse source exceeds label package limits")
    source_start, source_end, as_of = map(_time, (source.window_start, source.window_end, source.as_of))
    end = min(source_end, as_of)
    start = max(source_start, end - timedelta(days=days))
    if not source_start < source_end or not start < end:
        raise ValueError("source has no closed simulation window")
    articles = sorted(source.articles, key=lambda row: (row["tenant_id"], row["news_id"]))
    tenants = sorted({row["tenant_id"] for row in articles})
    if len(tenants) * users_per_tenant > MAX_TOTAL_USERS:
        raise ValueError("synthetic user limit exceeded")
    times = _snapshot_times(start, end)
    if len(times) * len(tenants) * users_per_tenant > MAX_PROFILE_ROWS:
        raise ValueError("profile snapshot limit exceeded")
    if len({(row["tenant_id"], row["news_id"]) for row in articles}) != len(articles):
        raise ValueError("source has duplicate news identities")
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
    connection = None
    try:
        connection = _create_index(work / "label_index.sqlite")
        (work / "policy.json").write_text(_json(policy.model_dump(mode="json")) + "\n", encoding="utf-8")
        counts = Counter(content_labels=0, news_behavior_labels=0, synthetic_events=0, synthetic_profiles=0)
        tagged = {}
        # 给文章打标签，写入文件和数据库
        with (work / "content_labels.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for article in articles:
                label = tag_article(article, policy)
                key = (label["tenant_id"], label["news_id"])
                tagged[key] = label
                payload = _json(label)
                stream.write(payload + "\n")
                connection.execute("INSERT INTO content_labels VALUES(?,?,?)", (*key, payload))
                counts["content_labels"] += 1
        # 根据观测数据生成行为标签，收集点击采样权重
        popularity = defaultdict(list)
        with (work / "news_behavior_labels.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for observation in sorted(source.observations, key=lambda row: (row["tenant_id"], row["news_id"], row["window_start"])):
                dataset_version = observation.get("source_dataset_version", source.source_identity.get("dataset_version", "single"))
                scenario_id = observation.get("scenario_id", dataset_version)
                definition = observation.get("metric_definition_id", observation.get("policy_version", "unspecified-source-policy"))
                label = behavior_labels({**observation, "source_dataset_version": dataset_version,
                                         "scenario_id": scenario_id, "metric_definition_id": definition})
                key = (label["tenant_id"], label["news_id"])
                if key not in tagged or label.get("content_sha256") != tagged[key]["content_sha256"]:
                    raise ValueError("behavior observation is not bound to the indexed article")
                payload = _json(label)
                stream.write(payload + "\n")
                connection.execute("INSERT INTO behavior_labels VALUES(?,?,?,?,?,?,?,?)", (*key, label["window_start"], label["window_end"], dataset_version, scenario_id, definition, payload))
                counts["news_behavior_labels"] += 1
                # Clicks are additive sampling weights; source UV is never added.
                if _time(label["window_end"]) <= end:
                    popularity[key[0]].append({"source_dataset_version": dataset_version,
                        "scenario_id": scenario_id, "metric_definition_id": definition,
                        "news_id": key[1], "clicks": label["clicks"],
                        "window_start": label["window_start"], "window_end": label["window_end"]})
        # 模拟用户行为数据和画像 1/topic_id做排序 2/取出当前租户的新闻标签后续模拟该租户的文章、标签和点击分布 3/
        roots = sorted(topic.topic_id for topic in policy.catalog.topics if topic.parent_topic_id is None and topic.enabled)
        with (work / "synthetic_events.jsonl").open("w", encoding="utf-8", newline="\n") as event_stream, \
             (work / "synthetic_profiles.jsonl").open("w", encoding="utf-8", newline="\n") as profile_stream:
            for tenant in tenants:
                labels = {news_id: value for (selected_tenant, news_id), value in tagged.items() if selected_tenant == tenant}
                events, grouped = _simulate_tenant(tenant, [row for row in articles if row["tenant_id"] == tenant], labels,
                    popularity[tenant], users=users_per_tenant, seed=seed, start=start, end=end, roots=roots)
                if counts["synthetic_events"] + len(events) > MAX_EVENTS:
                    raise ValueError("synthetic event limit exceeded")
                for event in events:
                    event_stream.write(_json(event) + "\n")
                counts["synthetic_events"] += len(events)
                # 根据行为计算不同时间点的画像
                for snapshot_at in times:
                    for user in sorted(grouped):
                        profile = profile_from_events(grouped[user], as_of=snapshot_at, root_ids=set(roots))
                        payload = _json(profile)
                        profile_stream.write(payload + "\n")
                        connection.execute("INSERT INTO profiles VALUES(?,?,?,?)", (tenant, user, profile["as_of"], payload))
                        counts["synthetic_profiles"] += 1
        # 完成数据包和发布
        manifest = {"schema_version": 1, "package_version": PACKAGE_VERSION,
            "source_identity": source.source_identity, "source_window": {"window_start": _iso(source_start), "window_end": _iso(source_end)},
            "simulation_window": {"window_start": _iso(start), "window_end": _iso(end)}, "build_as_of": _iso(as_of),
            "profile_snapshot_times": [_iso(item) for item in times], "tenants": tenants,
            "seed": seed, "users_per_tenant": users_per_tenant, "generator_version": GENERATOR_VERSION,
            "profile_version": PROFILE_VERSION, "taxonomy_version": policy.catalog.taxonomy_version,
            "policy_version": policy.policy_version, "behavior_rule_version": BEHAVIOR_RULE_VERSION,
            "profile_half_life_seconds": PROFILE_HALF_LIFE_SECONDS, "counts": dict(counts),
            "provenance": {"content_labels": "rule-inferred real-content topics; synthetic category fallback is explicit",
                "news_behavior_labels": "derived from isolated simulated news-window aggregates; no source UV sum",
                "synthetic_events": "independent synthetic world sampled using aggregate click distribution; not reconstructed users",
                "synthetic_profiles": "calculated only from synthetic events visible at each as_of; never enterprise user profiles"},
            "source_coverage_by_scenario": source.source_identity.get("coverage_by_scenario", []),
            "synthetic_world": "independent mixture of source-guided sampling distributions; does not reconstruct or combine original metric worlds"}
        metadata = {"manifest_core_json": _json(manifest), "package_version": PACKAGE_VERSION,
                    "taxonomy_version": policy.catalog.taxonomy_version, "policy_version": policy.policy_version,
                    "profile_version": PROFILE_VERSION, "build_as_of": _iso(as_of)}
        connection.executemany("INSERT INTO metadata VALUES(?,?)", sorted(metadata.items()))
        connection.commit()
        connection.execute("VACUUM")
        connection.close()
        connection = None
        artifacts = {}
        for name in ("policy.json", "content_labels.jsonl", "news_behavior_labels.jsonl", "synthetic_events.jsonl",
                     "synthetic_profiles.jsonl", "label_index.sqlite"):
            path = work / name
            artifacts[name] = {"sha256": _hash_file(path), "bytes": path.stat().st_size}
            if name.endswith(".jsonl"):
                artifacts[name]["rows"] = counts[name.removesuffix(".jsonl")]
        manifest["artifacts"] = artifacts
        manifest["identity_sha256"] = sha256(_json(manifest).encode("utf-8")).hexdigest()
        (work / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8")
        # Reserve the target atomically: another builder cannot replace a package
        # that appeared while these artifacts were being generated.
        output.mkdir()
        try:
            work.rename(output)
        except OSError:
            output.rmdir()
            raise
        return manifest
    # 资源清理
    finally:
        if connection is not None:
            connection.close()
        if work.exists():
            shutil.rmtree(work)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", action="append", required=True,
                        help="Repeat for additional frozen sources; explicit profile:NAME fixtures are also accepted")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--users-per-tenant", type=int, default=600)
    parser.add_argument("--seed", type=int, default=20261016)
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args(argv)
    try:
        manifest = build_package(args.source_dir, args.output_dir, policy_path=args.policy,
            users_per_tenant=args.users_per_tenant, seed=args.seed, days=args.days)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"Label package build failed: {exc}\n")
    print(_json({"output_dir": str(args.output_dir.absolute()), "identity_sha256": manifest["identity_sha256"],
                 "counts": manifest["counts"], "synthetic_profiles_only": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
