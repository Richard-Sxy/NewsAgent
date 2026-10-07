"""生成更大规模的模拟数据。支持每个租户 120，1200，12000条新闻，采用逐条生成方式，并计算数据校验摘要，供规模验证使用。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import json
from typing import Any, Iterator
from uuid import UUID

from app.sql_assistant.synthetic_profiles import enterprise_scenarios, _canonical


SCALED_PROFILE = "enterprise-v2"
SCALED_DATASET_VERSION = "news-enterprise-scaled-v2"
TABLES = ("dim_news", "news_metric_hourly", "news_metric_baseline_hourly")
ALLOWED_NEWS_COUNTS = (120, 1200, 12000)
DEFAULT_NEWS_COUNT = 1200
HOURS_PER_NEWS = 24
TENANTS = (UUID("11111111-1111-4111-8111-111111111111"), UUID("33333333-3333-4333-8333-333333333333"))
START = datetime(2026, 10, 3, tzinfo=timezone(timedelta(hours=8)))
_TYPES = ("article", "video", "article", "article", "video", "article", "video", "article", "video", "article", "video", "article")
_CATEGORIES = ("科技", "财经", "体育", "社会")
_TOPICS = ("城市交通服务观察", "智能设备应用观察", "低碳能源研究观察", "消费市场服务观察",
           "企业数字工具观察", "区域物流设施观察", "城市运动赛事观察", "青年体育活动观察",
           "社区运动服务观察", "公共阅读服务观察", "社区安全教育观察", "无障碍设施建设观察")


def validate_news_count(value: int) -> int:
    if type(value) is not int or value not in ALLOWED_NEWS_COUNTS:
        raise ValueError("scaled synthetic news count must be 120, 1200 or 12000")
    return value


def scaled_news_ids(news_per_tenant: int = DEFAULT_NEWS_COUNT) -> tuple[str, ...]:
    return tuple(f"scale-news-{index:06d}" for index in range(1, validate_news_count(news_per_tenant) + 1))


def scaled_scenarios() -> list[dict[str, Any]]:
    cases = enterprise_scenarios()
    for case in cases:
        case["question"] = "查询点击量最高的前100条新闻"
        case["row_limit"] = 100
        if case["id"] == "ranking-churn":
            case["description"] = "相同点击量Top100候选产生进入和退出；只比较共同新闻，不代表全数仓新闻走势。"
    return cases


def _normal(index: int) -> dict[str, int]:
    # Bounded independent variation avoids a monotonically growing tail that
    # would overwhelm the selected operational situations at larger scales.
    exposure = 150_000 + (index % 120) * 800 + (index // 120) * 100
    count = exposure * (10 + index % 7) // 100
    return _counters(exposure, count)


def _counters(exposure: int, count: int, *, duration: int = 45,
              effective: int = 65, interaction: int = 13) -> dict[str, int]:
    return {"impressions": exposure, "clicks": count, "unique_users": count * 83 // 100,
            "total_duration_seconds": count * duration, "effective_consumptions": count * effective // 100,
            "interactions": count * interaction // 100}


def _metrics(index: int, hour: int, count: int, *, content_type: str | None = None) -> dict[str, int]:
    normal = _normal(index)
    exposure, clicks = normal["impressions"], normal["clicks"]
    if hour in {0, 1}:
        factor = 100 if hour == 0 else 101
        return _counters(exposure * factor // 100, clicks * factor // 100)
    if hour in {2, 3}:
        if index <= 40:
            factor = 100 if hour == 2 else (600 if index % 4 == 0 else 110)
        else:
            factor = 1 + index % 3 if hour == 2 else (7 if index % 4 == 0 else 1 + index % 3)
        return _counters(exposure * factor, clicks * factor,
                         interaction=22 if hour == 3 and index % 4 == 0 else 13)
    if hour in {4, 5, 16, 18}:
        numerator = 25 if hour in {5, 16} else 100
        return _counters(exposure * numerator // 100, clicks * numerator // 100)
    if hour in {6, 7}:
        return normal if hour == 6 else _counters(exposure * 2, clicks * 110 // 100,
                                                  duration=12, effective=20, interaction=4)
    if hour in {8, 9}:
        video = (content_type or _TYPES[(index - 1) % 12]) == "video"
        if index <= 40:
            factor = (100 if video else 300) if hour == 8 else (400 if video else 100)
        else:
            factor = (1 + index % 2 if video else 3 + index % 4) if hour == 8 else (4 + index % 4 if video else 1 + index % 2)
        exposure *= factor
        return _counters(exposure, exposure * (25 if video else 8) // 100)
    if hour in {10, 11}:
        exposure = 1 + index % 3 + int(hour == 11) if index <= 50 else 0
        return _counters(exposure, exposure if index % 2 else exposure // 2, duration=12)
    if hour in {12, 13}:
        factor = 1 if hour == 12 else 2
        return _counters(exposure * factor, clicks * factor)
    if hour in {14, 15}:
        shift = 20 if count == 120 else 40
        leader = index <= 100 if hour == 14 else index <= 100 - shift or 101 <= index <= 100 + shift
        clicks = 40_000 + (200 - index) * 100 + (500 if hour == 15 else 0) if leader else 500 + index % 600
        return _counters(clicks * 8, clicks)
    if hour in {20, 21}:
        exposure = 10_000_000_000_000_001 + index * 97
        clicks = 1_000_000_000_000_001 + index * 29
        if hour == 21:
            exposure += 55_537 + index * 13
            clicks += 7_919 + index * 7
        return _counters(exposure, clicks, duration=29)
    return normal


def iter_scaled_rows(table: str, news_per_tenant: int = DEFAULT_NEWS_COUNT) -> Iterator[dict[str, Any]]:
    """Yield one physical row, in tenant/news/hour order, without an eager list."""
    count = validate_news_count(news_per_tenant)
    if table not in TABLES:
        raise ValueError("unsupported scaled fixture table")
    for tenant_index, tenant_id in enumerate(TENANTS):
        multiplier = 1 if tenant_index == 0 else 8
        for index in range(1, count + 1):
            news_id = f"scale-news-{index:06d}"
            if table == "dim_news":
                topic = _TOPICS[(index - 1) % 12]
                yield {"tenant_id": tenant_id, "news_id": news_id,
                       "title": f"[{'合成扩量' if tenant_index == 0 else '隔离扩量'}样本] {topic} 第{index:06d}期",
                       "content_type": _TYPES[(index - 1) % 12], "category": _CATEGORIES[((index - 1) // 3) % 4],
                       "source": f"合成观察中心-{1 + index % 5}",
                       "publish_time": START - timedelta(hours=12) + timedelta(seconds=index)}
                continue
            for hour in range(HOURS_PER_NEWS):
                row = {"tenant_id": tenant_id, "news_id": news_id, "event_time": START + timedelta(hours=hour)}
                if table == "news_metric_hourly":
                    values = _metrics(index, hour, count)
                    row.update({key: value * multiplier for key, value in values.items()})
                    row["total_duration_seconds"] = Decimal(row["total_duration_seconds"])
                else:
                    normal = _normal(index)
                    row.update({f"baseline_{key}": Decimal(0 if hour in {12, 13} else normal[key] * multiplier)
                                for key in ("impressions", "clicks", "effective_consumptions", "interactions")})
                yield row


class ScaledTableHasher:
    """Versioned line protocol; the complete row set is bound with its count."""
    def __init__(self, table: str):
        if table not in TABLES:
            raise ValueError("unsupported scaled fixture table")
        self._hash = sha256(f"news-scaled-fixture-v1\n{table}\n".encode())
        self.count = 0

    def update(self, row: dict) -> None:
        self._hash.update(json.dumps(_canonical(dict(row)), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode())
        self._hash.update(b"\n")
        self.count += 1

    def digest(self) -> str:
        finished = self._hash.copy()
        finished.update(f"rows:{self.count}\n".encode())
        return finished.hexdigest()


@lru_cache(maxsize=3)
def _cached_manifest(news_per_tenant: int) -> dict[str, Any]:
    validate_news_count(news_per_tenant)
    hashes = {}
    for table in TABLES:
        hasher = ScaledTableHasher(table)
        for row in iter_scaled_rows(table, news_per_tenant):
            hasher.update(row)
        hashes[table] = {"sha256": hasher.digest(), "rows": hasher.count}
    identity = {"dataset_profile": SCALED_PROFILE, "dataset_version": SCALED_DATASET_VERSION,
                "news_per_tenant": news_per_tenant, "hours_per_news": HOURS_PER_NEWS, "total_tenants": len(TENANTS),
                "hash_protocol": "news-scaled-fixture-v1", "tables": hashes, "enterprise_scenarios": scaled_scenarios()}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {**identity, "dataset_sha256": sha256(encoded).hexdigest()}


def scaled_manifest(news_per_tenant: int = DEFAULT_NEWS_COUNT) -> dict[str, Any]:
    # Only small manifest/catalog dictionaries are copied; no physical rows are
    # cached or exposed. Callers cannot mutate the internal identity cache.
    return deepcopy(_cached_manifest(validate_news_count(news_per_tenant)))
