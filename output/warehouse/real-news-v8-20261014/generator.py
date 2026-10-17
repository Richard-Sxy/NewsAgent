"""Independent v8 warehouse: real cached articles, explicitly simulated aggregates.

No Agent, database, vector-index, user-event, or enterprise-network connection.
Audience intervals are temporary mathematical sets, never persisted identities.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, Iterator

VERSION = "real-news-business-warehouse-v8-20261014"
SCHEMA = "news_sim_v8"
TENANTS = ("11111111-1111-4111-8111-111111111111", "33333333-3333-4333-8333-333333333333")
TZ = timezone(timedelta(hours=8))
DDL_PATH = Path(__file__).resolve().parents[2] / "docs/sql/real-news-warehouse-v8.sql"
POLICY_VERSION = "sim-click-attributed-reading-v8.1"
BASELINE_VERSION = "sim-prior-3-available-days-same-hour-v8.1"
UV_METHOD = "temporary_interval_union_v8.1"
CHANNELS = (("recommend", "推荐信息流", "app"), ("channel", "栏目页", "app"),
            ("search", "搜索结果", "app"), ("hot_board", "热榜入口", "app"),
            ("push", "消息入口", "app"), ("web", "网页入口", "web"))
ADDITIVE = ("impressions", "clicks", "detail_page_views", "direct_page_views", "total_duration_seconds",
            "effective_consumptions", "interactions", "likes", "comments", "shares",
            "favorites", "negative_feedback")
TABLE_NAMES = ("dim_behavior_policy", "dim_channel", "dim_news", "window_snapshot",
               "news_metric_5min", "news_metric_channel_hourly", "news_metric_hourly",
               "news_metric_daily", "news_metric_baseline_hourly", "content_change_event",
               "content_tombstone", "index_cleanup_task")


@dataclass(frozen=True)
class RealNewsSimulationConfig:
    start_at: str = "2026-09-30T00:00:00+08:00"
    closed_hours: int = 356
    seed: int = 20261014

    def __post_init__(self):
        if not isinstance(self.start_at, str):
            raise ValueError("start_at must be an aware ISO timestamp")
        try:
            value = datetime.fromisoformat(self.start_at)
        except ValueError as exc:
            raise ValueError("start_at must be an aware ISO timestamp") from exc
        if value.utcoffset() != timedelta(hours=8) or any((value.minute, value.second, value.microsecond)):
            raise ValueError("start_at must align to a whole hour in Asia/Shanghai (+08:00)")
        if type(self.closed_hours) is not int or not 1 <= self.closed_hours <= 31 * 24:
            raise ValueError("closed_hours must be an integer in [1,744]")
        if type(self.seed) is not int or not 0 <= self.seed <= 2**31 - 1:
            raise ValueError("seed must be an integer in [0,2147483647]")

    @property
    def start(self):
        return datetime.fromisoformat(self.start_at).astimezone(TZ)

    @property
    def end(self):
        return self.start + timedelta(hours=self.closed_hours)


SimulationConfig = RealNewsSimulationConfig


def union_size(intervals):
    total, end = 0, -1
    for left, right in sorted(intervals):
        if right < left:
            raise ValueError("invalid audience interval")
        total += max(0, right - max(left, end))
        end = max(end, right)
    return total


def copy_value(value):
    if value is None:
        return r"\N"
    if type(value) is bool:
        return "t" if value else "f"
    return str(value).replace("\\", "\\\\").replace("\t", r"\t").replace("\n", r"\n").replace("\r", r"\r")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value):
    if not isinstance(value, str):
        raise ValueError("catalog timestamp must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("catalog timestamp must be timezone aware")
    return parsed.astimezone(TZ)


def _catalog(catalog=None):
    if catalog is None:
        from app.warehouse_simulation.real_news_catalog import load_catalog
        catalog = load_catalog()
    articles = tuple(dict(article) for article in catalog)
    if not articles or len(articles) > 2000:
        raise ValueError("catalog must contain 1..2000 real articles")
    seen = set()
    for article in articles:
        required = ("news_id", "title", "content", "url", "publish_time", "category", "content_version")
        if any(name not in article for name in required):
            raise ValueError("catalog article is missing required real-content fields")
        if any(not isinstance(article[name], str) or not article[name].strip() or "\x00" in article[name]
               for name in required if name != "content_version"):
            raise ValueError("catalog real-content strings must be nonempty")
        if article["news_id"] in seen or not article["url"].startswith("https://"):
            raise ValueError("catalog contains duplicate IDs or non-HTTPS sources")
        if type(article["content_version"]) is not int or article["content_version"] < 1:
            raise ValueError("catalog content version must be a positive integer")
        if article.get("news_content_synthetic", False) is not False or article.get("content_type", "article") != "article":
            raise ValueError("v8 only accepts real text articles")
        _time(article["publish_time"])
        seen.add(article["news_id"])
    return tuple(sorted(articles, key=lambda item: item["news_id"]))


def _first_hour(article, config):
    published = _time(article["publish_time"])
    return max(0, int((published.replace(minute=0, second=0, microsecond=0) - config.start).total_seconds() // 3600))


def _eligible_bucket(article, stamp):
    published = _time(article["publish_time"])
    first = published.replace(minute=published.minute // 5 * 5, second=0, microsecond=0)
    return stamp >= first


def _channels(index):
    return tuple(CHANNELS[(index + step * 2) % len(CHANNELS)][0] for step in range(3))


def _noise(config, *parts):
    return int.from_bytes(hashlib.sha256(":".join(map(str, (config.seed, *parts))).encode()).digest()[:4], "big")


def _versions(config):
    for hour in range(config.closed_hours):
        yield hour, 1
        if hour == config.closed_hours - 1:
            yield hour, 2
        elif config.closed_hours >= 3 and hour == config.closed_hours - 2:
            yield hour, 2
    yield config.closed_hours, 1


def _lifecycle(article, index, tenant, config):
    # Only the isolated tenant exercises hypothetical lifecycle events.
    eligible = _time(article["publish_time"]) < config.end - timedelta(minutes=10)
    case = index % 24
    kind = {1: "withdraw", 2: "restrict", 3: "correct"}.get(case) if tenant == TENANTS[1] and eligible else None
    status = {"withdraw": "withdrawn", "restrict": "restricted"}.get(kind, "published")
    source_version = article["content_version"]
    version = source_version + (kind == "correct")
    return kind, status, int(version), 2 if kind else 1


def _dimension(article, index, tenant, config):
    kind, status, version, revision = _lifecycle(article, index, tenant, config)
    return {"tenant_id": tenant, "news_id": article["news_id"], "title": article["title"],
            "content": article["content"], "source_url": article["url"], "content_type": "article",
            "category": article["category"], "category_data_synthetic": article.get("category_data_synthetic", True),
            "source": article.get("source_label", "腾讯新闻公开页面缓存"),
            "publish_time": article["publish_time"], "publish_time_raw": article.get("publish_time_raw", article["publish_time"]),
            "source_content_version": article["content_version"], "content_version": article["content_version"],
            "fixture_content_version": version,
            "content_sha256": article.get("content_sha256", hashlib.sha256(article["content"].encode()).hexdigest()),
            "source_file": article.get("source_file", "catalog-injected-test"),
            "source_sha256": article.get("source_sha256", hashlib.sha256(_json(article).encode()).hexdigest()),
            "source_package": article.get("source_package", "catalog-injected-test"),
            "source_manifest_sha256": article.get("source_manifest_sha256"),
            "source_html_sha256": article.get("source_html_sha256"),
            "news_content_synthetic": False, "metric_data_synthetic": True,
            "lifecycle_data_synthetic": True, "lifecycle_scenario": kind or "none",
            "status": status, "lifecycle_revision": revision,
            "status_effective_at": (config.end - timedelta(minutes=10)).isoformat() if kind else article["publish_time"]}


def _snapshot(tenant, hour, revision, config):
    stamp = config.start + timedelta(hours=hour)
    incomplete = (hour == config.closed_hours or (hour == config.closed_hours - 1 and revision == 1)
                  or (config.closed_hours >= 3 and hour == config.closed_hours - 2 and revision == 2))
    watermark = stamp + (timedelta(minutes=30 if hour == config.closed_hours else 45) if incomplete else timedelta(hours=1, minutes=10))
    snapshot_at = watermark + timedelta(minutes=5)
    if hour == config.closed_hours - 1 and revision == 1:
        snapshot_at = stamp + timedelta(hours=1, minutes=5)
    if config.closed_hours >= 3 and hour == config.closed_hours - 2 and revision == 2:
        snapshot_at = config.end + timedelta(minutes=25)
    return {"tenant_id": tenant, "event_time": stamp.isoformat(), "snapshot_revision": revision,
            "window_end": (stamp + timedelta(hours=1)).isoformat(), "watermark_at": watermark.isoformat(),
            "snapshot_at": snapshot_at.isoformat(),
            "completeness": "incomplete" if incomplete else "corrected" if revision == 2 else "complete",
            "quality_status": "pending" if incomplete else "passed", "policy_version": POLICY_VERSION,
            "metric_data_synthetic": True}


def _aggregate(rows, click_intervals, visit_intervals, identity):
    row = dict(identity)
    row.update({name: sum(item[name] for item in rows) for name in ADDITIVE})
    row.update(unique_users=union_size(click_intervals), detail_visitors=union_size(visit_intervals))
    return row


def _hour_packet(article, index, tenant, hour, revision, config, daily_origin=0):
    stamp = config.start + timedelta(hours=hour)
    published = _time(article["publish_time"])
    first_bucket = published.replace(minute=published.minute // 5 * 5, second=0, microsecond=0)
    # Open hour is observed only to minute 35; exact publication controls bucket eligibility.
    bucket_count = 7 if hour == config.closed_hours else 12
    per_bin = {}
    channels, all_clicks, all_visits = [], [], []
    age = max(0, int((stamp - published).total_seconds() // 3600))
    decay = 4 if age < 24 else 3 if age < 72 else 2 if age < 168 else 1
    wave = 3 if stamp.hour in (8, 9, 12, 19, 20, 21) else 1
    case = index % 12
    if case == 4 and age < 48:
        wave *= 2
    if case == 5 and age >= 72:
        decay = 1
    for channel_number, channel in enumerate(_channels(index)):
        for bucket in range(bucket_count):
            bin_time = stamp + timedelta(minutes=5 * bucket)
            if bin_time < first_bucket:
                continue
            # Activity is only after actual publication within its containing bucket.
            seconds_available = min(300, max(0, int((bin_time + timedelta(minutes=5) - published).total_seconds())))
            clicks = (6 + _noise(config, tenant, article["news_id"], channel, hour, bucket) % 24) * wave * decay
            clicks = clicks * seconds_available // 300
            if case == 6:
                clicks = min(clicks, bucket % 2)
            if case == 7 and stamp.hour == 3:
                clicks = 0
            if hour == config.closed_hours - 1 and revision == 2 and bucket >= 9:
                clicks += 7 * seconds_available // 300
            impressions = clicks * (9 + channel_number) + _noise(config, "impressions", index, hour, bucket) % 31
            if case == 11 and stamp.hour == 4:
                clicks = impressions = 0  # Undefined CTR while direct detail access remains observable.
            if case == 8:
                impressions *= 3
            direct = 6 + _noise(config, "direct", index, hour, bucket, channel) % 9
            direct = direct * seconds_available // 300
            click_views = clicks * (13 if case == 8 else 9) // 10
            views = click_views + direct
            effective = min(clicks, click_views) * (35 if case == 9 else 72) // 100
            likes, comments, shares, favorites = clicks // 9, clicks // 23, clicks // 27, clicks // 31
            raw = {"impressions": impressions, "clicks": clicks, "detail_page_views": views, "direct_page_views": direct,
                   "total_duration_seconds": views * (20 + _noise(config, "duration", index, hour, bucket) % 27),
                   "effective_consumptions": effective, "interactions": likes + comments + shares + favorites,
                   "likes": likes, "comments": comments, "shares": shares, "favorites": favorites,
                   "negative_feedback": views // (8 if case == 10 else 113)}
            per_bin.setdefault(bucket, []).append((channel, raw))
    if not per_bin:
        return None
    # First generate counts, then construct day-scoped mathematical cohorts.
    # Per-channel UV is 85% of clicks / 80% of accesses (integer rounding applies).
    # Consecutive channels and bins overlap about 10%, preventing implausibly
    # tiny audiences when a peak has many clicks. No identity is generated.
    click_origin, visit_origin = daily_origin if isinstance(daily_origin, tuple) else (daily_origin, daily_origin)
    channel_sets = {channel: {"rows": [], "clicks": [], "visits": []} for channel in _channels(index)}
    fine = []
    for bucket, values in sorted(per_bin.items()):
        click_cursor, visit_cursor = click_origin, visit_origin
        bin_clicks, bin_visits, raw_rows = [], [], []
        for channel, raw in values:
            click_length, visit_length = raw["clicks"] * 85 // 100, raw["detail_page_views"] * 80 // 100
            click_interval = (click_cursor, click_cursor + click_length)
            visit_interval = (visit_cursor, visit_cursor + visit_length)
            bin_clicks.append(click_interval)
            bin_visits.append(visit_interval)
            raw_rows.append(raw)
            channel_sets[channel]["rows"].append(raw)
            channel_sets[channel]["clicks"].append(click_interval)
            channel_sets[channel]["visits"].append(visit_interval)
            click_cursor += max(1, click_length * 9 // 10) if click_length else 0
            visit_cursor += max(1, visit_length * 9 // 10) if visit_length else 0
        fine.append(_aggregate(raw_rows, bin_clicks, bin_visits,
                               {"tenant_id": tenant, "news_id": article["news_id"],
                                "event_time": (stamp + timedelta(minutes=5 * bucket)).isoformat(), "snapshot_revision": revision}))
        click_span = max(right for _left, right in bin_clicks) - click_origin
        visit_span = max(right for _left, right in bin_visits) - visit_origin
        click_origin += max(1, click_span * 9 // 10) if click_span else 0
        visit_origin += max(1, visit_span * 9 // 10) if visit_span else 0
        all_clicks.extend(bin_clicks)
        all_visits.extend(bin_visits)
    for channel, values in channel_sets.items():
        channels.append(_aggregate(values["rows"], values["clicks"], values["visits"],
                        {"tenant_id": tenant, "news_id": article["news_id"], "event_time": stamp.isoformat(),
                         "snapshot_revision": revision, "channel_id": channel}))
    total = _aggregate(channels, all_clicks, all_visits, {"tenant_id": tenant, "news_id": article["news_id"],
                           "event_time": stamp.isoformat(), "snapshot_revision": revision})
    total["uv_method"] = UV_METHOD
    total["policy_version"] = POLICY_VERSION
    return fine, channels, total, all_clicks, all_visits


def _baseline(article, tenant, hour, config, history):
    prior = [history[value] for offset in range(1, 4) if (value := hour - offset * 24) in history]
    row = {"tenant_id": tenant, "news_id": article["news_id"],
           "event_time": (config.start + timedelta(hours=hour)).isoformat(),
           "sample_count": len(prior), "baseline_version": BASELINE_VERSION}
    for name in ("impressions", "clicks", "effective_consumptions", "interactions"):
        row[f"baseline_{name}"] = str((Decimal(sum(item[name] for item in prior)) / len(prior)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if prior else None
    return row


def _article_rows(article, index, tenant, config):
    history, days = {}, {}
    daily_origins = {}
    first_hour = _first_hour(article, config)
    for hour, revision in _versions(config):
        if hour < first_hour:
            continue
        day = (config.start + timedelta(hours=hour)).date().isoformat()
        origin = daily_origins.get(day, (0, 0))
        packet = _hour_packet(article, index, tenant, hour, revision, config, daily_origin=origin)
        if packet is None:
            continue
        fine, channels, total, click_set, visit_set = packet
        for row in fine:
            yield "news_metric_5min", row
        for row in channels:
            yield "news_metric_channel_hourly", row
        yield "news_metric_hourly", total
        if revision == 1:
            yield "news_metric_baseline_hourly", _baseline(article, tenant, hour, config, history)
        complete = (hour < config.closed_hours and (hour < config.closed_hours - 1 or revision == 2)
                    and not (config.closed_hours >= 3 and hour == config.closed_hours - 2))
        if complete:
            history[hour] = total
            bundle = days.setdefault(day, {"rows": [], "clicks": [], "visits": [], "hours": []})
            bundle["rows"].append(total)
            bundle["clicks"].extend(click_set)
            bundle["visits"].extend(visit_set)
            bundle["hours"].append(hour)
            click_span = max(right for _left, right in click_set) - origin[0]
            visit_span = max(right for _left, right in visit_set) - origin[1]
            daily_origins[day] = (origin[0] + (max(1, click_span * 85 // 100) if click_span else 0),
                                  origin[1] + (max(1, visit_span * 85 // 100) if visit_span else 0))
    for day, bundle in sorted(days.items()):
        midnight = datetime.fromisoformat(day).replace(tzinfo=TZ)
        end = min(config.end, midnight + timedelta(days=1))
        start = max(config.start, midnight, _time(article["publish_time"]))
        expected_hours = sum(1 for hour in range(first_hour, config.closed_hours)
                             if (config.start + timedelta(hours=hour)).date().isoformat() == day)
        complete_day = (config.start <= midnight and config.end >= midnight + timedelta(days=1)
                        and len(bundle["hours"]) == expected_hours)
        row = _aggregate(bundle["rows"], bundle["clicks"], bundle["visits"],
                         {"tenant_id": tenant, "news_id": article["news_id"], "stat_date": day})
        row.update(coverage_start=start.isoformat(), coverage_end=end.isoformat(),
                   observed_hours=len(bundle["hours"]), completeness="complete" if complete_day else "partial",
                   excluded_hours=expected_hours - len(bundle["hours"]),
                   snapshot_revision=max(item["snapshot_revision"] for item in bundle["rows"]),
                   uv_method=UV_METHOD, policy_version=POLICY_VERSION)
        yield "news_metric_daily", row


def _changes(articles, config):
    tenant = TENANTS[1]
    for index, article in enumerate(articles):
        kind, status, version, revision = _lifecycle(article, index, tenant, config)
        if kind is None:
            continue
        key = hashlib.sha256(f"v8:{tenant}:{article['news_id']}:{kind}".encode()).hexdigest()
        yield {"tenant_id": tenant, "event_id": key, "news_id": article["news_id"], "event_type": kind,
               "from_version": article["content_version"], "to_version": version, "lifecycle_revision": revision,
               "new_status": status, "effective_at": (config.end - timedelta(minutes=10)).isoformat(),
               "received_at": (config.end - timedelta(minutes=9)).isoformat(),
               "reason_code": f"isolated_simulated_{kind}", "lifecycle_data_synthetic": True}


def _dimension_rows(articles, config):
    for tenant in TENANTS:
        yield "dim_behavior_policy", {"tenant_id": tenant, "policy_version": POLICY_VERSION,
              "click_definition": "deduplicated click events; repeat deliberate clicks count",
              "uv_definition": "unique_users=click UV; detail_visitors=independent detail-access UV",
              "pv_definition": "successful detail opens including direct entries; can exceed clicks",
              "effective_definition": "click-attributed effective reads, not all direct-entry reading",
              "metric_data_synthetic": True}
        for channel, label, platform in CHANNELS:
            yield "dim_channel", {"tenant_id": tenant, "channel_id": channel, "channel_name": label,
                                  "platform": platform, "dimension_data_synthetic": True}
        for index, article in enumerate(articles):
            yield "dim_news", _dimension(article, index, tenant, config)
        for hour, revision in _versions(config):
            yield "window_snapshot", _snapshot(tenant, hour, revision, config)
    for event in _changes(articles, config):
        yield "content_change_event", event
        if event["event_type"] != "correct":
            yield "content_tombstone", {"tenant_id": event["tenant_id"], "news_id": event["news_id"],
                  "event_id": event["event_id"], "blocked_through_version": event["to_version"],
                  "lifecycle_revision": event["lifecycle_revision"], "status": event["new_status"],
                  "effective_at": event["effective_at"], "lifecycle_data_synthetic": True}
        for target, action in (("postgres_knowledge", "deactivate_chunks"), ("milvus", "delete_versioned_chunks"),
                               ("bge_snapshot", "denylist_then_rebuild"), ("retrieval_cache", "invalidate")):
            yield "index_cleanup_task", {"tenant_id": event["tenant_id"],
                  "task_id": hashlib.sha256(f"{event['event_id']}:{target}".encode()).hexdigest(),
                  "event_id": event["event_id"], "news_id": event["news_id"], "target": target, "action": action,
                  "through_version": event["from_version"] if event["event_type"] == "correct" else event["to_version"],
                  "status": "pending", "attempt_count": 0, "mapping_required": True,
                  "lifecycle_data_synthetic": True, "created_at": event["received_at"]}


def iter_rows(table, config=RealNewsSimulationConfig(), catalog=None):
    if table not in TABLE_NAMES:
        raise ValueError("unknown real-news warehouse table")
    articles = _catalog(catalog)
    if table.startswith("news_metric_"):
        for tenant in TENANTS:
            for index, article in enumerate(articles):
                for name, row in _article_rows(article, index, tenant, config):
                    if name == table:
                        yield row
    else:
        for name, row in _dimension_rows(articles, config):
            if name == table:
                yield row


def scenario_catalog(config=RealNewsSimulationConfig(), catalog=None):
    articles = _catalog(catalog)
    labels = ("常规消费", "隔离租户模拟撤稿", "隔离租户模拟受限", "隔离租户模拟更正", "发布后短时升温",
              "新闻时效衰减", "低样本", "零点击", "曝光及直接访问", "点击归因阅读退化", "负反馈升高", "稳定长尾")
    return [{"scenario_id": f"metric-case-{index:02d}", "name": name,
             "news_id": articles[index % len(articles)]["news_id"], "metric_data_synthetic": True,
             "lifecycle_data_synthetic": True} for index, name in enumerate(labels)] + [
        {"scenario_id": "late-correction", "name": "最新修订先选再检验，最后闭合小时rev2纠正",
         "window_start": (config.end - timedelta(hours=1)).isoformat(), "metric_data_synthetic": True},
        {"scenario_id": "open-window", "name": "开放小时仅七个桶，禁止进入完整榜", "window_start": config.end.isoformat(), "metric_data_synthetic": True},
        {"scenario_id": "daily-uv", "name": "日点击UV与访问UV各自跨小时求并集，末日为partial", "metric_data_synthetic": True},
        {"scenario_id": "latest-invalidates-old-complete", "name": "倒数第二小时rev1完整、rev2不完整，禁止回退旧完整快照", "metric_data_synthetic": True},
        {"scenario_id": "publication-boundary", "name": "发布之前无行为、无基线，发布桶按剩余秒数计算", "metric_data_synthetic": True},
        {"scenario_id": "missing-baseline", "name": "只取真实存在的前三天同小时，零样本NULL", "metric_data_synthetic": True}]


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export_dataset(config, output_dir, catalog=None):
    """One metric pass, streaming disk spools, atomic new output directory only."""
    original_catalog = catalog is None
    articles = _catalog(catalog)
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("dataset output exists; choose a new directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    ddl = DDL_PATH.read_bytes()
    source_bytes = Path(__file__).read_bytes()
    with tempfile.TemporaryDirectory(prefix=".real-news-v8-", dir=output.parent) as temporary:
        work = Path(temporary) / "dataset"
        work.mkdir()
        (work / "generator.py").write_bytes(source_bytes)
        (work / "schema.sql").write_bytes(ddl)
        from app.warehouse_simulation import real_news_catalog as catalog_module
        loader_bytes = Path(catalog_module.__file__).read_bytes()
        (work / "catalog_loader.py").write_bytes(loader_bytes)
        catalog_bytes = catalog_module.CATALOG_PATH.read_bytes() if original_catalog else (_json(articles) + "\n").encode()
        (work / "catalog.json").write_bytes(catalog_bytes)
        handles = {name: (work / f"{name}.jsonl").open("w", encoding="utf-8", newline="\n") for name in TABLE_NAMES}
        copy_handles = {name: (work / f".{name}.copy").open("w", encoding="utf-8", newline="\n") for name in TABLE_NAMES}
        states = {name: {"hash": hashlib.sha256(), "rows": 0, "columns": None, "preview": []} for name in TABLE_NAMES}

        def write(name, row):
            state = states[name]
            columns = tuple(row)
            if state["columns"] is None:
                state["columns"] = columns
            elif state["columns"] != columns:
                raise ValueError("inconsistent table column order")
            line = _json(row) + "\n"
            handles[name].write(line)
            state["hash"].update(line.encode())
            copy_handles[name].write("\t".join(copy_value(row[key]) for key in columns) + "\n")
            state["rows"] += 1
            if len(state["preview"]) < 2:
                # Preview is bounded and contains a short excerpt, full real content remains in dim/catalog.
                state["preview"].append({**row, **({"content": row["content"][:250]} if "content" in row else {})})
        try:
            for name, row in _dimension_rows(articles, config):
                write(name, row)
            for tenant in TENANTS:
                for index, article in enumerate(articles):
                    for name, row in _article_rows(article, index, tenant, config):
                        write(name, row)
        finally:
            for handle in (*handles.values(), *copy_handles.values()):
                handle.close()
        sql_path = work / "warehouse.sql"
        with sql_path.open("w", encoding="utf-8", newline="\n") as sql:
            sql.write("\\set ON_ERROR_STOP on\nBEGIN;\nSET LOCAL TIME ZONE 'Asia/Shanghai';\n")
            sql.write(ddl.decode())
            for name in TABLE_NAMES:
                state = states[name]
                if state["columns"]:
                    sql.write(f"\nCOPY {SCHEMA}.{name} ({','.join(state['columns'])}) FROM STDIN;\n")
                    with (work / f".{name}.copy").open(encoding="utf-8") as spool:
                        for line in spool:
                            sql.write(line)
                    sql.write("\\.\n")
                (work / f".{name}.copy").unlink()
            sql.write("COMMIT;\n")
        tables = {name: {"rows": state["rows"], "sha256": state["hash"].hexdigest(), "columns": list(state["columns"] or ())}
                  for name, state in states.items()}
        manifest = {"dataset_version": VERSION, "schema": SCHEMA, "config": asdict(config), "tenants": list(TENANTS),
             "real_article_count": len(articles), "catalog_count": len(articles), "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
             "catalog_loader_sha256": hashlib.sha256(loader_bytes).hexdigest(),
             "schema_sha256": hashlib.sha256(ddl).hexdigest(), "generator_sha256": hashlib.sha256(source_bytes).hexdigest(),
             "closed_window_start": config.start.isoformat(), "closed_window_end": config.end.isoformat(),
             "historical_window_start": config.start.isoformat(), "historical_window_end": config.end.isoformat(),
             "start": config.start.isoformat(), "end": config.end.isoformat(), "asof": (config.end + timedelta(minutes=35)).isoformat(),
             "open_window_start": config.end.isoformat(), "tables": tables, "warehouse_sql_sha256": _file_sha(sql_path),
             "news_content_synthetic": False, "metric_data_synthetic": True, "category_data_synthetic": True,
             "lifecycle_data_synthetic": True,
             "content_provenance": "catalog preserves cached source IDs, title, body, URL and publication metadata",
             "metric_provenance": {"generator": "deterministic aggregate simulator", "policy_version": POLICY_VERSION,
                 "uv_method": UV_METHOD, "unique_users": "click UV", "detail_visitors": "independent successful-detail-access UV",
                 "detail_page_views": "successful detail opens including direct access; may exceed clicks",
                 "effective_consumptions": "click-attributed effective article reads",
                 "event_storage": "aggregate only; temporary audience intervals never saved"},
             "lifecycle_provenance": {"main_tenant": "all real articles remain published", "isolated_tenant": "hypothetical simulation only",
                  "cleanup_execution": "none; all tasks pending, no external adapter"},
             "integration_status": "standalone_fixture_not_connected_to_agent_or_vector_store"}
        manifest["dataset_sha256"] = hashlib.sha256(_json(manifest).encode()).hexdigest()
        for name, payload in (("manifest.json", manifest), ("preview.json", {name: state["preview"] for name, state in states.items()}),
                              ("scenarios.json", scenario_catalog(config, articles))):
            (work / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output.mkdir()
        try:
            work.rename(output)
        except OSError:
            output.rmdir()
            raise
    return manifest
