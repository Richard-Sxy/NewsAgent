"""
核心计算引擎。 validate_request() 校验输入， analyze() 根据操作类型执行统计。
使用整数 Decimal 计算，输出算法版本，来源快照哈希和结果。
"""

from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import re
from uuid import UUID


SCHEMA_VERSION = "1.0"
ALGORITHM_VERSION = "news-analysis-v1"
SCHEMA_VERSION_V2 = "2.0"
ALGORITHM_VERSION_V2 = "news-analysis-v2"
MAX_ROWS = 1_000
MAX_INTEGER = 2**63 - 1
_MAX_DECIMAL_LENGTH = 64
_CALCULATION_CONTEXT = Context(prec=64, rounding=ROUND_HALF_EVEN)
_CTR_TOLERANCE = Decimal("0.00005")
_OPERATIONS = frozenset({"overview", "distribution", "compare", "quality"})
_ADDITIVE_METRICS = (
    "impressions",
    "clicks",
    "total_duration_seconds",
    "effective_consumptions",
    "interactions",
)
_METRICS = frozenset((*_ADDITIVE_METRICS, "ctr", "hot_score"))
_REQUEST_KEYS = frozenset({"schema_version", "operation", "metric", "dataset"})
_V2_OPERATIONS = frozenset({"baseline", "trend"})
_V2_REQUEST_KEYS = _REQUEST_KEYS | {"reference_dataset"}
_DATASET_KEYS = frozenset(
    {"run_id", "window_start", "window_end", "bundle_version", "rows"}
)
_ROW_KEYS = frozenset(
    {
        "news_id",
        "rank",
        "content_type",
        *_ADDITIVE_METRICS,
        "unique_users",
        "ctr",
        "hot_score",
    }
)
_V2_DATASET_KEYS = _DATASET_KEYS | {"provenance"}
_PROVENANCE_KEYS = frozenset({"workflow_version", "selection_scope_sha256"})
_BASELINE_KEYS = frozenset(
    {"sample_count", "reference_version", *_ADDITIVE_METRICS, "ctr"}
)
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}", re.ASCII)
_DECIMAL_PATTERN = re.compile(r"[0-9]{1,20}(?:\.[0-9]{1,32})?", re.ASCII)
_DATETIME_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
    re.ASCII,
)
LIMITATIONS = (
    "仅分析当前 run 的上榜新闻，不代表全量新闻或全站指标。",
    "UV（unique_users）不可跨新闻相加，不能由该快照推断全站去重用户数。",
    "单个时间窗口不支持趋势判断或因果归因。",
)
_QUALITY_NOTES = (
    "CTR 核对采用四位小数舍入容差 0.00005；零曝光时按现有快照约定核对 CTR 为 0。",
    "点击数高于曝光数仅作为口径核对提示；重复点击、采集范围或事件定义可能影响比值，不能直接断言数据错误。",
)
_BASELINE_LIMITATIONS = (
    *LIMITATIONS[:2],
    "直接比较已保存的参考字段；样本窗口、选取策略和 CTR 口径需核对，不能将 sample_count 一概当作历史窗口数。",
    "参考字段可能是历史窗口均值或合成参考；不重算其 CTR，不将该比较当作实时趋势或因果证据。",
)
_TREND_LIMITATIONS = (
    *LIMITATIONS[:2],
    "仅比较两个窗口榜单中 news_id 相同且内容类型一致的交集；新增和退出榜单的新闻不参与聚合变化，不代表全量变化。",
    "双窗口比较只描述已保存指标的变化，不能据此作因果归因或推断持续趋势。",
    "gap_seconds 是参考窗口结束到当前窗口开始的间隔；大于 0 时窗口不连续。",
)


class AnalysisInputError(ValueError):
    """The input does not satisfy the bounded aggregate snapshot contract."""


def _object(value: object, *, keys: frozenset[str], path: str,
            optional: frozenset[str] = frozenset()) -> dict:
    if type(value) is not dict:
        raise AnalysisInputError(f"{path} must be an object")
    if any(type(key) is not str for key in value):
        raise AnalysisInputError(f"{path} keys must be strings")
    actual = frozenset(value)
    if actual - keys:
        # Do not echo untrusted keys, source text or secrets in diagnostic output.
        raise AnalysisInputError(f"{path} contains unknown fields")
    if (keys - optional) - actual:
        raise AnalysisInputError(f"{path} is missing required fields")
    return value


def _text(value: object, *, path: str, maximum: int = 128) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise AnalysisInputError(f"{path} must be a nonempty bounded string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise AnalysisInputError(f"{path} contains control characters")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise AnalysisInputError(f"{path} contains invalid Unicode")
    return value


def _integer(value: object, *, path: str, minimum: int = 0,
             maximum: int = MAX_INTEGER) -> int:
    # bool is an int subclass, but is not a count. Bounds avoid huge integer work.
    if type(value) is not int or not minimum <= value <= maximum:
        raise AnalysisInputError(f"{path} must be a bounded integer")
    return value


def _decimal_text(value: object, *, path: str, at_most_one: bool = False) -> str:
    if (
        type(value) is not str
        or len(value) > _MAX_DECIMAL_LENGTH
        or _DECIMAL_PATTERN.fullmatch(value) is None
    ):
        raise AnalysisInputError(f"{path} must be a bounded nonnegative decimal string")
    parsed = Decimal(value)
    if not parsed.is_finite() or (at_most_one and parsed > 1):
        raise AnalysisInputError(f"{path} is outside the allowed decimal range")
    return _format_decimal(parsed)


def _datetime(value: object, *, path: str) -> datetime:
    if (
        type(value) is not str
        or len(value) > 64
        or _DATETIME_PATTERN.fullmatch(value) is None
    ):
        raise AnalysisInputError(f"{path} must be a timezone-aware ISO datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Normalizing to UTC gives equivalent instants the same hash and output.
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise AnalysisInputError(f"{path} must be a valid ISO datetime") from exc


def _format_decimal(value: Decimal) -> str:
    # normalize() would use the caller's Decimal context. Fixed-point formatting
    # and string trimming instead preserve all computed significant digits.
    if value == 0:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _validate_request_v1(request: dict) -> dict:
    """Validate and copy a request into its canonical JSON-safe representation.

    Counts are signed-64-bit nonnegative integers, rank is in [1, 1000], and
    decimal strings have at most 20 integer and 32 fractional digits. Scientific
    notation, non-finite values, raw event details and unknown keys are rejected.
    This function never mutates the supplied request.
    """
    request = _object(
        request, keys=_REQUEST_KEYS, path="request", optional=frozenset({"metric"})
    )
    if type(request["schema_version"]) is not str or request["schema_version"] != SCHEMA_VERSION:
        raise AnalysisInputError("request.schema_version is unsupported")
    operation = request["operation"]
    if type(operation) is not str or operation not in _OPERATIONS:
        raise AnalysisInputError("request.operation is unsupported")
    metric = request.get("metric", "ctr")
    if type(metric) is not str or metric not in _METRICS:
        raise AnalysisInputError("request.metric is unsupported")

    dataset = _object(request["dataset"], keys=_DATASET_KEYS, path="dataset")
    run_id = _text(dataset["run_id"], path="dataset.run_id", maximum=36)
    try:
        parsed_run_id = UUID(run_id)
    except ValueError as exc:
        raise AnalysisInputError("dataset.run_id must be a UUID") from exc
    if len(run_id) != 36:
        raise AnalysisInputError("dataset.run_id must be a canonical UUID")
    window_start = _datetime(dataset["window_start"], path="dataset.window_start")
    window_end = _datetime(dataset["window_end"], path="dataset.window_end")
    if window_start >= window_end:
        raise AnalysisInputError("dataset window must be nonempty and left-closed/right-open")
    bundle_version = _text(dataset["bundle_version"], path="dataset.bundle_version")
    rows = dataset["rows"]
    if type(rows) is not list or len(rows) > MAX_ROWS:
        raise AnalysisInputError("dataset.rows must be a list of at most 1000 rows")

    canonical_rows = []
    news_ids: set[str] = set()
    ranks: set[int] = set()
    for row in rows:
        row = _object(row, keys=_ROW_KEYS, path="row")
        news_id = _text(row["news_id"], path="row.news_id")
        rank = _integer(row["rank"], path="row.rank", minimum=1, maximum=MAX_ROWS)
        if news_id in news_ids:
            raise AnalysisInputError("dataset.rows contains duplicate news_id")
        if rank in ranks:
            raise AnalysisInputError("dataset.rows contains duplicate rank")
        news_ids.add(news_id)
        ranks.add(rank)
        content_type = row["content_type"]
        if type(content_type) is not str or content_type not in {"article", "video"}:
            raise AnalysisInputError("row.content_type is unsupported")
        canonical_row = {"news_id": news_id, "rank": rank, "content_type": content_type}
        for field in (*_ADDITIVE_METRICS, "unique_users"):
            canonical_row[field] = _integer(row[field], path=f"row.{field}")
        canonical_row["ctr"] = _decimal_text(row["ctr"], path="row.ctr")
        canonical_row["hot_score"] = _decimal_text(
            row["hot_score"], path="row.hot_score", at_most_one=True
        )
        canonical_rows.append(canonical_row)
    canonical_rows.sort(key=lambda row: (row["rank"], row["news_id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "metric": metric,
        "dataset": {
            "run_id": str(parsed_run_id),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "bundle_version": bundle_version,
            "rows": canonical_rows,
        },
    }


def _validate_baseline(value: object) -> dict | None:
    if value is None:
        return None
    value = _object(value, keys=_BASELINE_KEYS, path="row.baseline")
    reference_version = value["reference_version"]
    if reference_version is not None:
        reference_version = _text(reference_version, path="row.baseline.reference_version")
    canonical = {
        "sample_count": _integer(
            value["sample_count"], path="row.baseline.sample_count", minimum=1
        ),
        "reference_version": reference_version,
    }
    for field in (*_ADDITIVE_METRICS, "ctr"):
        number = value[field]
        canonical[field] = (
            None if number is None
            else _decimal_text(number, path=f"row.baseline.{field}")
        )
    return canonical


def _validate_dataset_v2(value: object, *, include_baseline: bool, path: str) -> dict:
    dataset = _object(value, keys=_V2_DATASET_KEYS, path=path)
    provenance = _object(
        dataset["provenance"], keys=_PROVENANCE_KEYS, path=f"{path}.provenance"
    )
    workflow_version = _text(
        provenance["workflow_version"], path=f"{path}.provenance.workflow_version"
    )
    scope_hash = provenance["selection_scope_sha256"]
    if scope_hash is not None:
        if type(scope_hash) is not str or _SHA256_PATTERN.fullmatch(scope_hash) is None:
            raise AnalysisInputError(
                f"{path}.provenance.selection_scope_sha256 must be a SHA-256 string or null"
            )
        scope_hash = scope_hash.lower()
    rows = dataset["rows"]
    if type(rows) is not list or len(rows) > MAX_ROWS:
        raise AnalysisInputError(f"{path}.rows must be a list of at most 1000 rows")
    base_rows = []
    baselines = []
    row_keys = _ROW_KEYS | {"baseline"} if include_baseline else _ROW_KEYS
    for row in rows:
        row = _object(row, keys=row_keys, path=f"{path}.row")
        if include_baseline:
            baselines.append(_validate_baseline(row["baseline"]))
        base_rows.append({key: value for key, value in row.items() if key != "baseline"})
    # Reuse the established v1 row/date/bounds normalization without importing
    # siblings: this module must still load under the fixed runpy worker.
    base_dataset = {key: dataset[key] for key in _DATASET_KEYS if key != "rows"}
    base_dataset["rows"] = base_rows
    canonical = _validate_request_v1({
        "schema_version": SCHEMA_VERSION,
        "operation": "overview",
        "metric": "ctr",
        "dataset": base_dataset,
    })["dataset"]
    for field in ("window_start", "window_end"):
        if datetime.fromisoformat(canonical[field]).microsecond:
            raise AnalysisInputError(f"{path} window boundaries must use whole seconds")
    if include_baseline:
        baseline_by_id = {
            row["news_id"]: baseline for row, baseline in zip(base_rows, baselines)
        }
        for row in canonical["rows"]:
            row["baseline"] = baseline_by_id[row["news_id"]]
    canonical["provenance"] = {
        "workflow_version": workflow_version,
        "selection_scope_sha256": scope_hash,
    }
    return canonical


def _validate_trend_pair(current: dict, reference: dict) -> None:
    if current["run_id"] == reference["run_id"]:
        raise AnalysisInputError("trend requires different run IDs")
    if current["bundle_version"] != reference["bundle_version"]:
        raise AnalysisInputError("trend requires matching bundle versions")
    if current["provenance"]["workflow_version"] != reference["provenance"]["workflow_version"]:
        raise AnalysisInputError("trend requires matching workflow versions")
    current_scope = current["provenance"]["selection_scope_sha256"]
    reference_scope = reference["provenance"]["selection_scope_sha256"]
    if current_scope is None or reference_scope is None or current_scope != reference_scope:
        raise AnalysisInputError("trend requires matching nonnull selection scopes")
    current_start = datetime.fromisoformat(current["window_start"])
    current_end = datetime.fromisoformat(current["window_end"])
    reference_start = datetime.fromisoformat(reference["window_start"])
    reference_end = datetime.fromisoformat(reference["window_end"])
    if current_end - current_start != reference_end - reference_start:
        raise AnalysisInputError("trend requires equal window durations")
    if reference_end > current_start:
        raise AnalysisInputError("trend reference window must end before or at the current window start")
    reference_rows = {row["news_id"]: row for row in reference["rows"]}
    matched_count = 0
    for row in current["rows"]:
        reference_row = reference_rows.get(row["news_id"])
        if reference_row is None:
            continue
        if row["content_type"] != reference_row["content_type"]:
            raise AnalysisInputError("trend matched news must have the same content type")
        matched_count += 1
    if not matched_count:
        raise AnalysisInputError("trend requires a nonempty news intersection")


def _validate_request_v2(request: dict) -> dict:
    request = _object(request, keys=_V2_REQUEST_KEYS, path="request",
                      optional=frozenset({"reference_dataset"}))
    if type(request["schema_version"]) is not str or request["schema_version"] != SCHEMA_VERSION_V2:
        raise AnalysisInputError("request.schema_version is unsupported")
    operation = request["operation"]
    if type(operation) is not str or operation not in _V2_OPERATIONS:
        raise AnalysisInputError("request.operation is unsupported for schema 2.0")
    metric = request["metric"]
    if type(metric) is not str or metric not in _METRICS:
        raise AnalysisInputError("request.metric is unsupported")
    canonical = {
        "schema_version": SCHEMA_VERSION_V2,
        "operation": operation,
        "metric": metric,
        "dataset": _validate_dataset_v2(
            request["dataset"], include_baseline=operation == "baseline", path="dataset"
        ),
    }
    if operation == "baseline":
        if "reference_dataset" in request:
            raise AnalysisInputError("baseline does not accept a reference dataset")
    else:
        if "reference_dataset" not in request:
            raise AnalysisInputError("trend is missing the required reference dataset")
        reference = _validate_dataset_v2(
            request["reference_dataset"], include_baseline=False, path="reference_dataset"
        )
        _validate_trend_pair(canonical["dataset"], reference)
        canonical["reference_dataset"] = reference
    return canonical


def validate_request(request: dict) -> dict:
    """Return the exact versioned, bounded aggregate request without mutation.

    Version 1 remains unchanged. Version 2 compares saved baseline fields or
    compatible windows and rejects any raw fields outside its numeric whitelist.
    """
    if type(request) is dict and request.get("schema_version") == SCHEMA_VERSION_V2:
        return _validate_request_v2(request)
    return _validate_request_v1(request)


def _overview(rows: list[dict]) -> dict:
    totals = {field: sum(row[field] for row in rows) for field in _ADDITIVE_METRICS}
    weighted_ctr = (
        _format_decimal(Decimal(totals["clicks"]) / Decimal(totals["impressions"]))
        if totals["impressions"] else None
    )
    return {"totals": totals, "weighted_ctr": weighted_ctr}


def _distribution(rows: list[dict], metric: str) -> dict:
    values = sorted(Decimal(row[metric]) for row in rows)
    count = len(values)
    if not count:
        return {"count": 0, "min": None, "max": None, "mean": None,
                "median": None, "p90": None}
    midpoint = count // 2
    median = values[midpoint] if count % 2 else (values[midpoint - 1] + values[midpoint]) / 2
    # Nearest-rank p90 = ceil(0.9 * N), expressed with exact integer arithmetic.
    p90_index = (9 * count + 9) // 10 - 1
    return {
        "count": count,
        "min": _format_decimal(values[0]),
        "max": _format_decimal(values[-1]),
        "mean": _format_decimal(sum(values, Decimal(0)) / count),
        "median": _format_decimal(median),
        "p90": _format_decimal(values[p90_index]),
    }


def _compare(rows: list[dict], metric: str) -> dict:
    if not rows:
        return {"highest": None, "lowest": None, "absolute_difference": None,
                "relative_change": None}
    # Rows already follow authoritative rank; ties select the earliest rank.
    highest = max(rows, key=lambda row: Decimal(row[metric]))
    lowest = min(rows, key=lambda row: Decimal(row[metric]))
    high_value = Decimal(highest[metric])
    low_value = Decimal(lowest[metric])
    difference = high_value - low_value
    return {
        "highest": {"news_id": highest["news_id"], "value": _format_decimal(high_value)},
        "lowest": {"news_id": lowest["news_id"], "value": _format_decimal(low_value)},
        "absolute_difference": _format_decimal(difference),
        "relative_change": _format_decimal(difference / low_value) if low_value else None,
    }


def _quality(rows: list[dict]) -> dict:
    zero_impressions = []
    ctr_mismatches = []
    clicks_above_impressions = []
    for row in rows:
        impressions = row["impressions"]
        clicks = row["clicks"]
        if impressions == 0:
            zero_impressions.append(row["news_id"])
        expected_ctr = Decimal(clicks) / Decimal(impressions) if impressions else Decimal(0)
        if abs(Decimal(row["ctr"]) - expected_ctr) > _CTR_TOLERANCE:
            ctr_mismatches.append(row["news_id"])
        if clicks > impressions:
            clicks_above_impressions.append(row["news_id"])
    return {
        "zero_impression_news_ids": zero_impressions,
        "ctr_mismatch_news_ids": ctr_mismatches,
        "clicks_above_impressions_news_ids": clicks_above_impressions,
        "notes": list(_QUALITY_NOTES),
    }


def _change(current: Decimal | None, reference: Decimal | None) -> dict:
    if current is None or reference is None:
        absolute_change = None
        relative_change = None
    else:
        absolute_change = _format_decimal(current - reference)
        relative_change = (
            _format_decimal((current - reference) / reference) if reference else None
        )
    return {
        "current_value": None if current is None else _format_decimal(current),
        "reference_value": None if reference is None else _format_decimal(reference),
        "absolute_change": absolute_change,
        "relative_change": relative_change,
    }


def _metric_value(row: dict, metric: str) -> Decimal | None:
    # A stored zero CTR in a zero-exposure snapshot is an input convention, not
    # a measured rate; comparisons must not turn it into a claimed decline.
    if metric == "ctr" and row["impressions"] is not None and Decimal(row["impressions"]) == 0:
        return None
    value = row.get(metric)
    return None if value is None else Decimal(value)


def _baseline(rows: list[dict], metric: str) -> dict:
    items = []
    missing_baseline = []
    missing_metric = []
    compared_count = 0
    for row in rows:
        baseline = row["baseline"]
        current_value = _metric_value(row, metric)
        reference_value = None if baseline is None else _metric_value(baseline, metric)
        if baseline is None:
            status = "missing_baseline"
            missing_baseline.append(row["news_id"])
        elif current_value is None or reference_value is None:
            status = "missing_metric"
            missing_metric.append(row["news_id"])
        else:
            status = "compared"
            compared_count += 1
        items.append({
            "news_id": row["news_id"],
            **_change(current_value, reference_value),
            "sample_count": None if baseline is None else baseline["sample_count"],
            "reference_version": None if baseline is None else baseline["reference_version"],
            "status": status,
        })
    return {
        "compared_count": compared_count,
        "missing_baseline_news_ids": missing_baseline,
        "missing_metric_news_ids": missing_metric,
        "items": items,
    }


def _aggregate_value(rows: list[dict], metric: str) -> Decimal | None:
    if metric == "ctr":
        impressions = sum(row["impressions"] for row in rows)
        return (
            Decimal(sum(row["clicks"] for row in rows)) / Decimal(impressions)
            if impressions else None
        )
    total = sum((Decimal(row[metric]) for row in rows), Decimal(0))
    return total / len(rows) if metric == "hot_score" else total


def _trend(current: dict, reference: dict, metric: str) -> dict:
    reference_by_id = {row["news_id"]: row for row in reference["rows"]}
    current_ids = {row["news_id"] for row in current["rows"]}
    current_rows = [row for row in current["rows"] if row["news_id"] in reference_by_id]
    reference_rows = [reference_by_id[row["news_id"]] for row in current_rows]
    items = [
        {"news_id": row["news_id"],
         **_change(_metric_value(row, metric), _metric_value(reference_row, metric))}
        for row, reference_row in zip(current_rows, reference_rows)
    ]
    gap = (
        datetime.fromisoformat(current["window_start"])
        - datetime.fromisoformat(reference["window_end"])
    )
    return {
        "matched_news_count": len(current_rows),
        "added_news_ids": [
            row["news_id"] for row in current["rows"] if row["news_id"] not in reference_by_id
        ],
        "removed_news_ids": [
            row["news_id"] for row in reference["rows"] if row["news_id"] not in current_ids
        ],
        "gap_seconds": gap.days * 86400 + gap.seconds,
        "items": items,
        "aggregate": {
            "method": "weighted_ctr" if metric == "ctr" else (
                "mean" if metric == "hot_score" else "sum"
            ),
            **_change(
                _aggregate_value(current_rows, metric),
                _aggregate_value(reference_rows, metric),
            ),
        },
    }


def _source(dataset: dict) -> dict:
    serialized = json.dumps(
        dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    source = {
        "run_id": dataset["run_id"],
        "window_start": dataset["window_start"],
        "window_end": dataset["window_end"],
        "bundle_version": dataset["bundle_version"],
        "row_count": len(dataset["rows"]),
        "scope": "ranked_news_only",
        "snapshot_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }
    if "provenance" in dataset:
        source["provenance"] = dict(dataset["provenance"])
    return source


def analyze(request: dict) -> dict:
    """Return deterministic JSON-safe findings and their exact snapshot source."""
    canonical = validate_request(request)
    operation = canonical["operation"]
    metric = canonical["metric"]
    dataset = canonical["dataset"]
    rows = dataset["rows"]
    is_v2 = canonical["schema_version"] == SCHEMA_VERSION_V2
    source = _source(dataset)
    limitations = LIMITATIONS
    with localcontext(_CALCULATION_CONTEXT):
        if operation == "baseline":
            analysis = _baseline(rows, metric)
            limitations = _BASELINE_LIMITATIONS
        elif operation == "trend":
            analysis = _trend(dataset, canonical["reference_dataset"], metric)
            source["reference"] = _source(canonical["reference_dataset"])
            limitations = _TREND_LIMITATIONS
        elif operation == "overview":
            analysis = _overview(rows)
        elif operation == "distribution":
            analysis = _distribution(rows, metric)
        elif operation == "compare":
            analysis = _compare(rows, metric)
        else:
            analysis = _quality(rows)
    return {
        "schema_version": SCHEMA_VERSION_V2 if is_v2 else SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION_V2 if is_v2 else ALGORITHM_VERSION,
        "operation": operation,
        "metric": metric,
        "source": source,
        "analysis": analysis,
        "limitations": list(limitations),
    }
