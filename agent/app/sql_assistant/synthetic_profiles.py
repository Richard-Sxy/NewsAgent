"""生成业务场景模拟数据。包括流量突增、热度回落、消费漏斗退化、零曝光、小样本等情况，并生成数据指纹。这些的都是合成数据。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any
from uuid import UUID


CLASSIC_PROFILE = "classic-v1"
ENTERPRISE_PROFILE = "enterprise-v1"
ENTERPRISE_DATASET_VERSION = "news-enterprise-scenarios-v1"
_START = datetime(2026, 10, 3, tzinfo=timezone(timedelta(hours=8)))
_QUESTION = "查询点击量最高的前12条新闻"


def validate_profile(value: str) -> str:
    if type(value) is not str or value not in {CLASSIC_PROFILE, ENTERPRISE_PROFILE, "enterprise-v2", "public-headlines-v3"}:
        raise ValueError("unsupported synthetic dataset profile")
    return value


def enterprise_scenarios() -> list[dict[str, Any]]:
    """Return fresh bounded configuration, with no runtime or mutable IDs."""
    definitions = (
        ("steady", "稳定流量", "正常流量轻微波动，观察稳定同口径指标。", 0, 1, 12,
         ["stable_traffic"]),
        ("breaking", "突发流量增长", "部分新闻曝光和点击显著增加；数字变化不证明外部事件因果。", 2, 3, 12,
         ["clicks_growth", "traffic_concentration"]),
        ("fatigue", "热度回落", "曝光与消费回落，比较同一组新闻的下降幅度。", 4, 5, 12,
         ["clicks_decline"]),
        ("funnel", "消费漏斗退化", "曝光增加，但点击率与有效消费比例降低；需分别核对分母。", 6, 7, 12,
         ["ctr_decline", "effective_consumption_decline"]),
        ("content-mix", "内容流量结构变化", "图文与视频曝光占比改变，单篇点击率稳定；整体CTR采用次数加权。", 8, 9, 12,
         ["content_mix_change", "weighted_ctr"]),
        ("low-volume", "零曝光与小样本", "包含零曝光、个位数曝光和高CTR小样本；不据此推断可靠性或因果。", 10, 11, 12,
         ["zero_impressions", "small_sample"]),
        ("zero-baseline", "已知零基线", "基线明确保存为0，区别于未知或缺失；相对变化无有效分母。", 12, 13, 12,
         ["known_zero_baseline", "undefined_relative_change"]),
        ("ranking-churn", "候选榜单进出", "相同点击量Top5查询产生进入和退出；趋势仅比较共同新闻。", 14, 15, 5,
         ["cohort_change", "matched_cohort_only"]),
        ("recovery-gap", "间隔窗口恢复", "比较16点与18点，明确中间有一小时空档；不声称连续走势。", 16, 18, 12,
         ["non_contiguous", "clicks_recovery"]),
        ("precision", "大数精度", "合法计数超过JavaScript安全整数；Python和SQL保留精确整数与Decimal。", 20, 21, 12,
         ["exact_large_integers"]),
    )
    return [
        {"id": identity, "label": label, "description": description,
         "question": _QUESTION if limit == 12 else "查询点击量最高的前5条新闻",
         "reference_start": (_START + timedelta(hours=reference)).isoformat(),
         "current_start": (_START + timedelta(hours=current)).isoformat(),
         "current_end": (_START + timedelta(hours=current + 1)).isoformat(),
         "row_limit": limit, "expected_signals": list(signals)}
        for identity, label, description, reference, current, limit, signals in definitions
    ]


def _normal(index: int) -> dict[str, int]:
    impressions = 120_000 + index * 4_000
    clicks = impressions * (10 + index % 5) // 100
    return {"impressions": impressions, "clicks": clicks,
            "unique_users": clicks * 83 // 100,
            "total_duration_seconds": clicks * 45,
            "effective_consumptions": clicks * 65 // 100,
            "interactions": clicks * 13 // 100}


def _counters(impressions: int, clicks: int, *, duration: int = 45,
              effective_percent: int = 65, interaction_percent: int = 13) -> dict[str, int]:
    return {"impressions": impressions, "clicks": clicks,
            "unique_users": clicks * 83 // 100, "total_duration_seconds": clicks * duration,
            "effective_consumptions": clicks * effective_percent // 100,
            "interactions": clicks * interaction_percent // 100}


def _hour_values(index: int, hour: int, content_type: str) -> dict[str, int] | None:
    normal = _normal(index)
    impressions, clicks = normal["impressions"], normal["clicks"]
    if hour in {0, 1}:
        factor = 100 if hour == 0 else 101
        return _counters(impressions * factor // 100, clicks * factor // 100)
    if hour in {2, 3}:
        factor = 100 if hour == 2 else (600 if index <= 3 else 110)
        return _counters(impressions * factor // 100, clicks * factor // 100,
                         interaction_percent=22 if hour == 3 and index <= 3 else 13)
    if hour in {4, 5}:
        factor = 100 if hour == 4 else 25
        return _counters(impressions * factor // 100, clicks * factor // 100)
    if hour in {6, 7}:
        return (_counters(impressions, clicks) if hour == 6 else
                _counters(impressions * 2, clicks * 110 // 100, duration=12,
                          effective_percent=20, interaction_percent=4))
    if hour in {8, 9}:
        factor = (300 if hour == 8 else 70) if content_type == "article" else (50 if hour == 8 else 500)
        exposure = impressions * factor // 100
        return _counters(exposure, exposure * (8 if content_type == "article" else 25) // 100)
    if hour in {10, 11}:
        # Some zero denominators and some very small high-CTR observations.
        exposure = 0 if index % 4 == 0 else 1 + index % 3
        if hour == 11 and index % 4:
            exposure += 1
        return _counters(exposure, exposure if index % 2 else exposure // 2, duration=12)
    if hour in {12, 13}:
        factor = 1 if hour == 12 else 2
        return _counters(impressions * factor, clicks * factor)
    if hour in {14, 15}:
        leaders = {1: 9_000, 2: 8_000, 3: 7_000, 4: 6_000, 5: 5_000} if hour == 14 else {
            1: 11_000, 2: 10_000, 6: 9_000, 7: 8_000, 8: 7_000,
        }
        count = leaders.get(index, 1_000 + index)
        return _counters(count * 8, count)
    if hour in {16, 18}:
        factor = 25 if hour == 16 else 100
        return _counters(impressions * factor // 100, clicks * factor // 100)
    if hour in {20, 21}:
        exposure = 10_000_000_000_000_001 + index * 97
        count = 1_000_000_000_000_001 + index * 29
        if hour == 21:
            exposure += 55_537 + index * 13
            count += 7_919 + index * 7
        return _counters(exposure, count, duration=29)
    return None  # Preserve classic 17/19 and the original 22/23 demonstration.


def enterprise_fixture_rows(classic_rows: tuple[list[dict], list[dict], list[dict]]) -> tuple[list[dict], list[dict], list[dict]]:
    """Replace values in a copy, never change v1 IDs, cardinality or source rows."""
    news, metrics, baselines = deepcopy(classic_rows)
    types = {(row["tenant_id"], row["news_id"]): row["content_type"] for row in news}
    # The first tenant is the established demonstration tenant. Other tenants
    # retain the existing visible x8 isolation canary, without raw user records.
    primary = news[0]["tenant_id"]
    for row, baseline in zip(metrics, baselines):
        index = int(row["news_id"].rsplit("-", 1)[1])
        hour = int((row["event_time"] - _START).total_seconds()) // 3600
        values = _hour_values(index, hour, types[(row["tenant_id"], row["news_id"])])
        if values is None:
            continue
        multiplier = 1 if row["tenant_id"] == primary else 8
        row.update({key: number * multiplier for key, number in values.items()})
        row["total_duration_seconds"] = Decimal(row["total_duration_seconds"])
        reference = _normal(index)
        for key in ("impressions", "clicks", "effective_consumptions", "interactions"):
            number = 0 if hour in {12, 13} else reference[key] * multiplier
            baseline[f"baseline_{key}"] = Decimal(number).quantize(Decimal("0.01"))
    return news, metrics, baselines


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        fixed = format(value, "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def fixture_fingerprint(rows: tuple[list[dict], list[dict], list[dict]]) -> str:
    """Hash all physical rows, independent of query ordering and timezone spelling."""
    tables = []
    for table in rows:
        normalized = [_canonical(dict(row)) for row in table]
        tables.append(sorted(normalized, key=lambda row: (row["tenant_id"], row["news_id"], row.get("event_time", ""))))
    encoded = json.dumps(tables, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode()).hexdigest()


def enterprise_manifest(rows: tuple[list[dict], list[dict], list[dict]]) -> dict[str, Any]:
    identity = {"dataset_profile": ENTERPRISE_PROFILE,
                "dataset_version": ENTERPRISE_DATASET_VERSION,
                "fixture_sha256": fixture_fingerprint(rows),
                "enterprise_scenarios": enterprise_scenarios()}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {"dataset_profile": ENTERPRISE_PROFILE, "dataset_version": ENTERPRISE_DATASET_VERSION,
            "dataset_sha256": sha256(encoded.encode()).hexdigest(),
            "enterprise_scenarios": enterprise_scenarios()}
