"""Thirty-day deterministic news warehouse, separate from frozen v1–v3 data."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import json

from app.sql_assistant.scaled_profiles import (
    TABLES, TENANTS, ScaledTableHasher, _metrics, _normal, scaled_scenarios,
    iter_scaled_rows, validate_news_count,
)

TIMELINE_PROFILE = "timeline-v4"
TIMELINE_VERSION = "news-timeline-20260910-20261009-v1"
START = datetime(2026, 9, 10, tzinfo=timezone(timedelta(hours=8)))
END = datetime(2026, 10, 10, tzinfo=START.tzinfo)
HOURS = 720
TOPICS = {
    "科技": ("人工智能工具应用", "国产芯片研发", "机器人场景测试", "卫星通信试验", "新能源汽车技术", "智能手机新品观察", "数据安全研究", "开源软件应用", "量子计算研究", "工业数字化", "低空飞行技术", "新能源储能"),
    "财经": ("国庆消费市场", "旅游订单变化", "制造企业经营", "跨境贸易观察", "物流运价变化", "地方消费补贴", "能源价格观察", "中小企业融资", "零售业经营", "汽车市场需求", "绿色产业投资", "就业市场服务"),
    "体育": ("足球联赛赛程", "篮球联赛训练", "城市马拉松筹备", "网球赛事观察", "青年游泳赛事", "校园运动活动", "全民健身服务", "冰雪运动备赛", "自行车赛事路线", "排球赛事训练", "体育场馆开放", "运动健康研究"),
    "社会": ("假期出行服务", "城市交通疏导", "社区养老服务", "公共安全演练", "极端天气应对", "学校食堂管理", "公共阅读活动", "医疗预约服务", "无障碍设施建设", "社区便民改造", "生态环境监测", "公共文化展览"),
}


def timeline_scenarios():
    cases = scaled_scenarios()
    # Existing edge cases are available on the most recent sample day.
    for case in cases:
        for field in ("reference_start", "current_start", "current_end"):
            case[field] = (datetime.fromisoformat(case[field]) + timedelta(days=6)).isoformat()
    for identity, label, previous, current, signal in (
        ("day-over-day", "跨日同小时", 28, 29, "daily_change"),
        ("week-over-week", "跨周同小时", 22, 29, "weekly_change"),
        ("month-span", "月内首尾窗口", 0, 29, "long_range_change"),
    ):
        cases.append(dict(id=identity, label=label,
            description="比较同一小时的两个独立窗口；仅比较共同新闻，不推断中间连续走势或跨日去重人数。",
            question="查询点击量最高的前100条新闻", row_limit=100,
            reference_start=(START + timedelta(days=previous, hours=19)).isoformat(),
            current_start=(START + timedelta(days=current, hours=19)).isoformat(),
            current_end=(START + timedelta(days=current, hours=20)).isoformat(),
            expected_signals=[signal, "matched_cohort_only"]))
    return cases


def iter_timeline_rows(table, news_per_tenant=1200):
    count = validate_news_count(news_per_tenant)
    if table not in TABLES:
        raise ValueError("unsupported timeline table")
    if table == "dim_news":
        for row in iter_scaled_rows(table, count):
            index = int(row["news_id"].rsplit("-", 1)[1])
            row["publish_time"] = START - timedelta(hours=12) + timedelta(seconds=index)
            topic = TOPICS[row["category"]][((index - 1) // 12) % 12]
            row["title"] = f"[合成热点样本] {topic} · 观察记录{index:06d}"
            yield row
        return
    for tenant_index, tenant in enumerate(TENANTS):
        isolation = 1 if tenant_index == 0 else 8
        for index in range(1, count + 1):
            for offset in range(HOURS):
                day, hour = divmod(offset, 24)
                # Daily levels and weekly seasonality; rotating breaking cohorts.
                factor = 80 + day * 2 + (20 if (START + timedelta(days=day)).weekday() >= 5 else 0)
                hot = (index + day * 7) % 31 < 3
                burst = 3 if hot and hour in {17, 19, 22, 23} else 1
                row = dict(tenant_id=tenant, news_id=f"scale-news-{index:06d}",
                           event_time=START + timedelta(hours=offset))
                if table == "news_metric_hourly":
                    values = _metrics(index, hour, count)
                    row.update({key: value * factor // 100 * burst * isolation for key, value in values.items()})
                    row["total_duration_seconds"] = Decimal(row["total_duration_seconds"])
                else:
                    normal = _normal(index)
                    row.update({f"baseline_{key}": Decimal(0 if hour in {12, 13} else normal[key] * factor // 100 * isolation)
                                for key in ("impressions", "clicks", "effective_consumptions", "interactions")})
                yield row


@lru_cache(maxsize=3)
def _manifest(count):
    hashes = {}
    for table in TABLES:
        hasher = ScaledTableHasher(table)
        for row in iter_timeline_rows(table, count):
            hasher.update(row)
        hashes[table] = dict(sha256=hasher.digest(), rows=hasher.count)
    identity = dict(dataset_profile=TIMELINE_PROFILE, dataset_version=TIMELINE_VERSION,
                    news_per_tenant=count, hours_per_news=HOURS, total_tenants=2,
                    window_start=START.isoformat(), window_end=END.isoformat(),
                    hash_protocol="news-scaled-fixture-v1", tables=hashes,
                    enterprise_scenarios=timeline_scenarios())
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {**identity, "dataset_sha256": sha256(encoded).hexdigest()}


def timeline_manifest(news_per_tenant=1200):
    return deepcopy(_manifest(validate_news_count(news_per_tenant)))
