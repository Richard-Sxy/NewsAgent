"""Versioned Tencent News business simulation, using no user-level event records.

Public product concepts inform the dimensions. All values and lifecycle events
are synthetic. This module deliberately has no connection to the active v1-v6
warehouse, model Ports, vector stores, or enterprise credentials.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

VERSION = "tencent-news-business-simulation-v7-20261014"
SCHEMA = "news_sim_v7"
TENANTS = ("11111111-1111-4111-8111-111111111111", "33333333-3333-4333-8333-333333333333")
TZ = timezone(timedelta(hours=8))
DDL_PATH = Path(__file__).resolve().parents[2] / "docs/sql/tencent-news-simulation-v7.sql"
CHANNELS = (
    ("recommend", "推荐信息流", "app"), ("channel", "栏目页", "app"),
    ("search", "搜索结果", "app"), ("hot_board", "热榜入口", "app"),
    ("push", "消息推送", "app"), ("web", "网页入口", "web"),
)
TOPICS = (
    ("科技", "人工智能", "产业发展观察"), ("科技", "数字生活", "终端应用体验"),
    ("科技", "航天科普", "科研进展解读"), ("财经", "宏观经济", "公开数据释义"),
    ("财经", "产业公司", "产业动态跟踪"), ("财经", "消费市场", "市场供需观察"),
    ("体育", "球类赛事", "比赛过程复盘"), ("体育", "全民健身", "运动场景观察"),
    ("体育", "综合赛事", "赛事规则解读"), ("社会", "城市民生", "公共服务进展"),
    ("社会", "天气出行", "出行信息提示"), ("社会", "事实核查", "公开说法查证"),
)
ADDITIVE = (
    "impressions", "clicks", "total_duration_seconds", "effective_consumptions",
    "interactions", "likes", "comments", "shares", "favorites", "follows",
    "negative_feedback", "video_starts", "video_completions",
)
TABLE_NAMES = (
    "dim_publisher", "dim_channel", "dim_news", "window_snapshot",
    "news_metric_channel_hourly", "news_metric_hourly", "news_metric_5min",
    "news_metric_baseline_hourly", "content_change_event", "content_tombstone",
    "index_cleanup_task", "board_snapshot", "board_item",
)


@dataclass(frozen=True)
class SimulationConfig:
    news_per_tenant: int = 120
    days: int = 7
    start_date: str = "2026-10-07"
    seed: int = 20261014

    def __post_init__(self) -> None:
        for name, minimum, maximum in (("news_per_tenant", 12, 1200), ("days", 1, 30), ("seed", 0, 2**31 - 1)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
        if self.news_per_tenant % 12:
            raise ValueError("news_per_tenant must be a multiple of 12 for scenario coverage")
        if not isinstance(self.start_date, str):
            raise ValueError("start_date must be YYYY-MM-DD")
        try:
            parsed = datetime.strptime(self.start_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("start_date must be YYYY-MM-DD") from exc
        if parsed.strftime("%Y-%m-%d") != self.start_date:
            raise ValueError("start_date must be YYYY-MM-DD")

    @property
    def start(self) -> datetime:
        return datetime.strptime(self.start_date, "%Y-%m-%d").replace(tzinfo=TZ)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(days=self.days)


def _id(index: int) -> str:
    return f"sim-tn-v7-{index + 1:05d}"


def _noise(config: SimulationConfig, *parts: Any) -> int:
    value = ":".join(str(part) for part in (config.seed, *parts))
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:4], "big")


def union_size(intervals: list[tuple[int, int]]) -> int:
    """Count a synthetic audience union without producing or retaining identities."""
    total, end = 0, -1
    for left, right in sorted(intervals):
        if right < left:
            raise ValueError("invalid audience interval")
        total += max(0, right - max(left, end))
        end = max(end, right)
    return total


def _content(tenant: str, index: int, config: SimulationConfig) -> dict[str, Any]:
    category, topic, subject = TOPICS[index % len(TOPICS)]
    case = index % 12
    status = "withdrawn" if case == 1 else "restricted" if case == 2 else "published"
    fmt = ("article", "video", "gallery", "live_text")[index % 4]
    return {
        "tenant_id": tenant, "news_id": _id(index),
        "title": f"【合成】{topic}·{subject}（案例{index + 1:03d}）",
        "content_type": "video" if fmt == "video" else "article", "content_format": fmt,
        "category": category, "primary_topic": topic, "event_id": f"sim-event-{index % 12:02d}",
        "publisher_id": f"sim-publisher-{index % 6:02d}", "source": f"合成内容来源{index % 6 + 1}",
        "publish_time": (config.start - timedelta(hours=1 + index % 48)).isoformat(),
        "content_version": 2 if case == 3 else 1,
        "lifecycle_revision": 2 if case in (1, 2, 3) else 1, "status": status,
        "status_effective_at": (config.end - timedelta(minutes=10)).isoformat() if case in (1, 2, 3) else config.start.isoformat(),
        "content_source": "tencent-news-simulation-v7", "synthetic": True,
    }


def _selected_channels(index: int) -> tuple[str, ...]:
    return tuple(CHANNELS[(index + offset * 2) % len(CHANNELS)][0] for offset in range(3))


def _versions(config: SimulationConfig) -> Iterator[tuple[int, int]]:
    for hour in range(config.days * 24):
        yield hour, 1
        if hour == config.days * 24 - 1:
            yield hour, 2
    yield config.days * 24, 1


def _snapshot(tenant: str, hour: int, revision: int, config: SimulationConfig) -> dict[str, Any]:
    start = config.start + timedelta(hours=hour)
    is_open = hour == config.days * 24
    last = hour == config.days * 24 - 1
    incomplete = is_open or (last and revision == 1)
    watermark = start + timedelta(minutes=30 if is_open else 45) if incomplete else start + timedelta(hours=1, minutes=10)
    return {
        "tenant_id": tenant, "event_time": start.isoformat(), "snapshot_revision": revision,
        "window_end": (start + timedelta(hours=1)).isoformat(),
        "watermark_at": watermark.isoformat(), "snapshot_at": (watermark + timedelta(minutes=5)).isoformat(),
        "completeness": "incomplete" if incomplete else "corrected" if revision == 2 else "complete",
        "late_corrected_bins": config.news_per_tenant * 9 if revision == 2 else 0,
        "late_added_clicks": config.news_per_tenant * 63 if revision == 2 else 0,
        "metric_policy_version": "sim-consumption-policy-v1", "dataset_version": VERSION,
        "quality_status": "pending" if incomplete else "passed", "synthetic": True,
    }


def _channel_buckets(tenant: str, index: int, channel: str, hour: int, revision: int,
                     config: SimulationConfig) -> tuple[list[dict[str, Any]], list[tuple[int, int]]]:
    """Produce aggregate bins; interval arithmetic models repeat/cross-channel consumers."""
    buckets, audiences = [], []
    channel_number = next(i for i, item in enumerate(CHANNELS) if item[0] == channel)
    is_open = hour == config.days * 24
    count = 7 if is_open else 12
    content = _content(tenant, index, config)
    day = hour // 24
    hour_of_day = hour % 24
    wave = 3 if hour_of_day in (8, 9, 12, 19, 20, 21) else 1
    if (config.start + timedelta(days=day)).weekday() >= 5 and content["category"] == "体育":
        wave += 1
    if index % 12 == 4 and hour >= (config.days * 24) // 2:
        wave *= 3  # developing event
    if index % 12 == 5 and hour >= (config.days * 24) // 2:
        wave = 1  # cooling event
    cursor = channel_number * 19
    for bucket in range(count):
        clicks = (18 + _noise(config, tenant, index, channel, hour, bucket) % 43) * wave
        if index % 12 == 6:
            clicks = bucket % 2  # low sample
        if index % 12 == 7 and hour_of_day == 3:
            clicks = 0  # true zero; not missing
        if revision == 2 and bucket >= 9:
            clicks += 7  # deterministic late correction
        impressions = clicks * (10 + channel_number) + (_noise(config, "exposure", index, hour, bucket) % 71)
        if index % 12 == 8:
            impressions *= 3  # exposure increase without proportional clicks
        effective = clicks * (35 if index % 12 == 9 else 72) // 100
        likes, comments = clicks // 8, clicks // 19
        shares, favorites, follows = clicks // 23, clicks // 29, clicks // 37
        length = clicks * 7 // 10
        audience = (cursor, cursor + length)
        audiences.append(audience)
        cursor += max(0, length // 2)
        seconds = 65 if content["content_type"] == "video" else 32
        buckets.append({
            "tenant_id": tenant, "news_id": _id(index), "channel_id": channel,
            "event_time": (config.start + timedelta(hours=hour, minutes=5 * bucket)).isoformat(),
            "snapshot_revision": revision, "impressions": impressions, "clicks": clicks,
            "unique_users": length, "total_duration_seconds": clicks * seconds,
            "effective_consumptions": effective, "interactions": likes + comments + shares + favorites + follows,
            "likes": likes, "comments": comments, "shares": shares, "favorites": favorites, "follows": follows,
            "negative_feedback": clicks // (9 if index % 12 == 10 else 101),
            "video_starts": clicks if content["content_type"] == "video" else 0,
            "video_completions": effective * 3 // 4 if content["content_type"] == "video" else 0,
        })
    return buckets, audiences


def _hour_metrics(tenant: str, index: int, hour: int, revision: int,
                  config: SimulationConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    channels, all_audiences = [], []
    for channel in _selected_channels(index):
        buckets, audiences = _channel_buckets(tenant, index, channel, hour, revision, config)
        row = {name: buckets[0][name] for name in ("tenant_id", "news_id", "channel_id", "event_time", "snapshot_revision")}
        row.update({name: sum(item[name] for item in buckets) for name in ADDITIVE})
        row["unique_users"] = union_size(audiences)
        channels.append(row)
        all_audiences.extend(audiences)
    total = {name: channels[0][name] for name in ("tenant_id", "news_id", "event_time", "snapshot_revision")}
    total.update({name: sum(item[name] for item in channels) for name in ADDITIVE})
    total["unique_users"] = union_size(all_audiences)
    total["uv_method"] = "synthetic_interval_union_v1"
    return channels, total


def _changes(tenant: str, config: SimulationConfig) -> Iterator[dict[str, Any]]:
    for index in range(config.news_per_tenant):
        case = index % 12
        if case not in (1, 2, 3):
            continue
        content = _content(tenant, index, config)
        kind = {1: "withdraw", 2: "restrict", 3: "correct"}[case]
        event_id = hashlib.sha256(f"{tenant}:{content['news_id']}:2:{kind}".encode()).hexdigest()
        yield {"tenant_id": tenant, "event_id": event_id, "news_id": content["news_id"],
               "event_type": kind, "from_version": 1, "to_version": content["content_version"],
               "lifecycle_revision": 2, "new_status": content["status"],
               "effective_at": content["status_effective_at"], "received_at": (config.end - timedelta(minutes=9)).isoformat(),
               "reason_code": f"synthetic_{kind}_case", "synthetic": True}


def iter_rows(table: str, config: SimulationConfig = SimulationConfig()) -> Iterator[dict[str, Any]]:
    if table not in TABLE_NAMES:
        raise ValueError(f"unknown simulation table: {table}")
    for tenant in TENANTS:
        if table == "dim_publisher":
            for index, kind in enumerate(("newsroom", "wire", "regional_media", "vertical_media", "creator", "fact_check")):
                yield {"tenant_id": tenant, "publisher_id": f"sim-publisher-{index:02d}",
                       "publisher_name": f"合成内容来源{index + 1}", "publisher_kind": kind, "synthetic": True}
        elif table == "dim_channel":
            for channel, name, platform in CHANNELS:
                yield {"tenant_id": tenant, "channel_id": channel, "channel_name": name, "platform": platform, "synthetic": True}
        elif table == "dim_news":
            for index in range(config.news_per_tenant):
                yield _content(tenant, index, config)
        elif table == "window_snapshot":
            for hour, revision in _versions(config):
                yield _snapshot(tenant, hour, revision, config)
        elif table in ("news_metric_hourly", "news_metric_channel_hourly", "news_metric_5min"):
            for index in range(config.news_per_tenant):
                for hour, revision in _versions(config):
                    if table == "news_metric_5min":
                        if hour < config.days * 24 - 1:
                            continue
                        for channel in _selected_channels(index):
                            yield from _channel_buckets(tenant, index, channel, hour, revision, config)[0]
                    else:
                        channels, total = _hour_metrics(tenant, index, hour, revision, config)
                        if table == "news_metric_hourly":
                            yield total
                        else:
                            yield from channels
        elif table == "news_metric_baseline_hourly":
            for index in range(config.news_per_tenant):
                for hour in range(config.days * 24 + 1):
                    prior = [hour - 24 * offset for offset in range(1, 4) if hour - 24 * offset >= 0]
                    historical = [_hour_metrics(tenant, index, value, 1, config)[1] for value in prior]
                    row = {"tenant_id": tenant, "news_id": _id(index),
                           "event_time": (config.start + timedelta(hours=hour)).isoformat(),
                           "sample_count": len(prior), "baseline_version": "sim-prior-3-days-same-hour-v1"}
                    for name in ("impressions", "clicks", "effective_consumptions", "interactions"):
                        # Fixed decimal strings avoid float drift; no samples means NULL.
                        numerator = sum(item[name] for item in historical)
                        row[f"baseline_{name}"] = str((Decimal(numerator) / len(prior)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if prior else None
                    yield row
        elif table == "content_change_event":
            yield from _changes(tenant, config)
        elif table == "content_tombstone":
            for event in _changes(tenant, config):
                if event["event_type"] == "correct":
                    continue
                yield {"tenant_id": tenant, "news_id": event["news_id"], "blocked_through_version": event["to_version"],
                       "lifecycle_revision": event["lifecycle_revision"],
                       "event_id": event["event_id"], "status": event["new_status"],
                       "effective_at": event["effective_at"], "policy_version": "sim-content-eligibility-v1"}
        elif table == "index_cleanup_task":
            for event in _changes(tenant, config):
                for target, action in (("postgres_knowledge", "deactivate_chunks"), ("milvus", "delete_versioned_chunks"),
                                       ("bge_snapshot", "denylist_then_rebuild"), ("retrieval_cache", "invalidate")):
                    key = hashlib.sha256(f"{event['event_id']}:{target}".encode()).hexdigest()
                    yield {"tenant_id": tenant, "task_id": key, "event_id": event["event_id"],
                           "news_id": event["news_id"], "content_source": "tencent-news-simulation-v7",
                           "target": target, "action": action,
                           "through_version": event["from_version"] if event["event_type"] == "correct" else event["to_version"],
                           "lifecycle_revision": event["lifecycle_revision"],
                           "status": "pending", "attempt_count": 0, "mapping_required": True,
                           "created_at": event["received_at"]}
        elif table == "board_snapshot":
            yield {"tenant_id": tenant, "board_id": "sim-editorial-board", "snapshot_id": "sim-board-final-v1",
                   "snapshot_at": (config.end + timedelta(minutes=1)).isoformat(),
                   "board_policy_version": "sim-editorial-order-v1", "source_kind": "synthetic_original_board", "synthetic": True}
        elif table == "board_item":
            # Deliberately different from CTR/hotness; preserve ties/gaps and original positions.
            for position, index in enumerate(reversed(range(min(12, config.news_per_tenant))), 1):
                yield {"tenant_id": tenant, "board_id": "sim-editorial-board", "snapshot_id": "sim-board-final-v1",
                       "news_id": _id(index), "source_rank": position if position != 3 else 2,
                       "source_position": position, "source_score": f"{100 - position}.00"}


def scenario_catalog(config: SimulationConfig = SimulationConfig()) -> list[dict[str, Any]]:
    labels = ("常规消费", "撤稿后立即屏蔽", "权限受限", "正文更正与旧版本清理", "事件升温", "事件回落",
              "低样本", "零点击", "曝光升高点击不同比增长", "有效消费退化", "负反馈升高", "事实核查专题")
    return [{"scenario_id": f"sim-case-{index:02d}", "name": label, "news_id": _id(index), "synthetic": True}
            for index, label in enumerate(labels)] + [
        {"scenario_id": "late-correction", "name": "最后闭合小时迟到修订", "window_start": (config.end - timedelta(hours=1)).isoformat(), "revisions": [1, 2], "synthetic": True},
        {"scenario_id": "open-window", "name": "进行中窗口禁止进入完整小时榜", "window_start": config.end.isoformat(), "synthetic": True},
        {"scenario_id": "cross-channel-uv", "name": "同小时跨渠道去重人数不能相加", "synthetic": True},
        {"scenario_id": "missing-baseline", "name": "历史样本不足保留NULL与样本数", "synthetic": True},
        {"scenario_id": "original-board", "name": "原榜保序、并列与过滤后保留名次", "synthetic": True},
    ]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def copy_value(value: Any) -> str:
    """Encode PostgreSQL text COPY data; data cannot become a SQL instruction."""
    if value is None:
        return r"\N"
    if type(value) is bool:
        return "t" if value else "f"
    return str(value).replace("\\", "\\\\").replace("\t", r"\t").replace("\n", r"\n").replace("\r", r"\r")


def export_dataset(config: SimulationConfig, output_dir: Path) -> dict[str, Any]:
    """Create a new artifact directory atomically; never replace an existing dataset."""
    output_dir = Path(output_dir).absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError("dataset output already exists; choose a new directory")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    ddl = DDL_PATH.read_bytes()
    source_bytes = Path(__file__).read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    tables, preview = {}, {}
    with tempfile.TemporaryDirectory(prefix=".tencent-news-v7-", dir=output_dir.parent) as temporary:
        work = Path(temporary) / "dataset"
        work.mkdir()
        (work / "generator.py").write_bytes(source_bytes)
        (work / "schema.sql").write_bytes(ddl)
        sql_path = work / "warehouse.sql"
        with sql_path.open("w", encoding="utf-8", newline="\n") as sql:
            sql.write("\\set ON_ERROR_STOP on\nBEGIN;\nSET LOCAL TIME ZONE 'Asia/Shanghai';\n")
            sql.write(ddl.decode("utf-8"))
            for table in TABLE_NAMES:
                digest, count, examples = hashlib.sha256(), 0, []
                columns = None
                with (work / f"{table}.jsonl").open("w", encoding="utf-8", newline="\n") as data:
                    for row in iter_rows(table, config):
                        if columns is None:
                            columns = tuple(row)
                            sql.write(f"\nCOPY {SCHEMA}.{table} ({','.join(columns)}) FROM STDIN;\n")
                        elif tuple(row) != columns:
                            raise ValueError("inconsistent simulation columns")
                        line = _json(row) + "\n"
                        data.write(line)
                        digest.update(line.encode("utf-8"))
                        sql.write("\t".join(copy_value(row[name]) for name in columns) + "\n")
                        count += 1
                        if len(examples) < 3:
                            examples.append(row)
                if columns:
                    sql.write("\\.\n")
                tables[table] = {"rows": count, "sha256": digest.hexdigest()}
                preview[table] = examples
            sql.write("COMMIT;\n")
        manifest = {
            "dataset_version": VERSION, "schema": SCHEMA, "synthetic": True, "config": asdict(config),
            "tenants": list(TENANTS), "historical_window_start": config.start.isoformat(),
            "historical_window_end": config.end.isoformat(), "open_window_start": config.end.isoformat(),
            "schema_sha256": hashlib.sha256(ddl).hexdigest(), "generator_sha256": source_sha,
            "tables": tables, "warehouse_sql_sha256": _file_sha256(sql_path),
            "integration_status": "standalone_fixture_not_connected_to_agent_or_vector_store",
        }
        manifest["dataset_sha256"] = hashlib.sha256(_json(manifest).encode()).hexdigest()
        for name, payload in (("manifest.json", manifest), ("preview.json", preview), ("scenarios.json", scenario_catalog(config))):
            (work / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Reserve the destination exclusively before replacing our own empty directory.
        # This also catches another exporter finishing while this one was generating.
        output_dir.mkdir()
        try:
            work.rename(output_dir)
        except OSError:
            output_dir.rmdir()
            raise
    return manifest


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
