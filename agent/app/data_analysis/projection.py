"""
将热点报告转换成分析数据集。project_dataset() 提取新闻指标、热度及可选基线，检查运行状态、新闻身份和时间窗口；
跨窗口分析还会校验筛选规则的来源。
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from uuid import UUID


class AnalysisProjectionError(ValueError):
    pass


_COUNTS = ("impressions", "clicks", "unique_users", "total_duration_seconds",
           "effective_consumptions", "interactions")
_BASELINE_METRICS = ("impressions", "clicks", "effective_consumptions", "interactions", "ctr", "total_duration_seconds")
_BASELINE_KEYS = {"news_id", "content_type", "sample_count", "reference_version", "unique_users", *_BASELINE_METRICS}
_HASH = re.compile(r"[a-f0-9]{64}")


def _fail():
    raise AnalysisProjectionError("analysis_snapshot_identity_mismatch")


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail()
    return value.astimezone(timezone.utc)


def _parse_time(value):
    if not isinstance(value, str) or len(value) > 64:
        _fail()
    try:
        return _utc(datetime.fromisoformat(value))
    except ValueError:
        _fail()


def _baseline(raw, news_id, content_type):
    if raw is None:
        return None
    if (not isinstance(raw, dict) or set(raw) - _BASELINE_KEYS
            or raw.get("news_id") != news_id or raw.get("content_type") != content_type
            or type(raw.get("sample_count")) is not int or raw["sample_count"] < 1):
        _fail()
    reference = raw.get("reference_version")
    if reference is not None and (not isinstance(reference, str) or not reference.strip() or len(reference) > 128):
        _fail()
    result = {"sample_count": raw["sample_count"], "reference_version": reference}
    for key in _BASELINE_METRICS:
        value = raw.get(key)
        if value is None:
            result[key] = None
            continue
        if type(value) not in {str, int} or len(str(value)) > 64:
            _fail()
        try:
            number = Decimal(value)
            if not number.is_finite() or number < 0:
                _fail()
            # Bound exponents before converting persisted Decimal scientific notation.
            if not -32 <= number.as_tuple().exponent <= 20:
                _fail()
            result[key] = format(number, "f")
        except InvalidOperation:
            _fail()
    return result


def selection_scope(detail, *, tenant_id):
    """Freeze selection semantics, excluding user question, identity and window.

    Historical non-SQL runs have no frozen query policy, so their scope is unknown.
    No similarity of returned news IDs can substitute for an approved scope.
    """
    trace = detail.sql_tool_trace
    if trace is None:
        return None
    try:
        preview, result = trace.preview, trace.result
        if not (trace.query_id == preview.query_id == result.query_id):
            _fail()
        if (not _HASH.fullmatch(preview.sql_hash) or sha256(preview.sql.encode()).hexdigest() != preview.sql_hash
                or result.sql_hash != preview.sql_hash or result.truncated
                or result.row_count != len(result.rows)):
            _fail()
        if not _HASH.fullmatch(preview.schema_sha256):
            _fail()
        params = preview.parameters
        if set(params) - {"tenant_id", "window_start", "window_end", "row_limit", "content_type", "category"}:
            _fail()
        if (UUID(str(params["tenant_id"])) != UUID(tenant_id)
                or _parse_time(params["window_start"]) != _utc(detail.run.window_start)
                or _parse_time(params["window_end"]) != _utc(detail.run.window_end)
                or type(params["row_limit"]) is not int or not 1 <= params["row_limit"] <= 1000
                or result.row_count > params["row_limit"]):
            _fail()
        candidate_ids = {row["news_id"] for row in result.rows}
        if len(candidate_ids) != len(result.rows) or not {item.news_id for item in detail.ranked_news} <= candidate_ids:
            _fail()
        content_type = params.get("content_type")
        if "content_type" in params and content_type not in {"article", "video"}:
            _fail()
        candidates = {row["news_id"]: row for row in result.rows}
        for item in detail.ranked_news:
            candidate = candidates[item.news_id]
            if (content_type is not None and item.metrics.content_type != content_type
                    or "content_type" in candidate and candidate["content_type"] != item.metrics.content_type
                    or "category" in params and "category" in candidate and candidate["category"] != params["category"]):
                _fail()
        supplemental_hash = trace.supplemental_sql_hash
        if trace.supplemental_sql is None or supplemental_hash is None:
            # Missing supplemental metric mapping is not comparable provenance.
            return None
        if (not _HASH.fullmatch(supplemental_hash)
                or sha256(trace.supplemental_sql.encode()).hexdigest() != supplemental_hash):
            _fail()
        identity = {
            "version": "analysis-selection-v1", "schema_version": preview.schema_version,
            "schema_sha256": preview.schema_sha256, "scenario_id": preview.scenario_id,
            "sql_sha256": preview.sql_hash, "supplemental_sql_sha256": supplemental_hash,
            "parameters": {key: value for key, value in params.items()
                           if key not in {"tenant_id", "window_start", "window_end"}},
        }
        return sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError):
        _fail()


def project_dataset(detail, *, run_id, tenant_id, extended=False, include_baseline=False):
    """Project the entire immutable ranked snapshot; never truncate it for analysis."""
    if detail is None or detail.run.status != "completed":
        raise AnalysisProjectionError("analysis_completed_run_not_found")
    if (detail.run.run_id != UUID(run_id)
            or type(detail.run.ranked_news_count) is not int
            or detail.run.ranked_news_count != len(detail.ranked_news)):
        _fail()
    rows = []
    for item in detail.ranked_news:
        metrics = item.metrics
        if (metrics.news_id != item.news_id or _utc(metrics.window_start) != _utc(detail.run.window_start)
                or _utc(metrics.window_end) != _utc(detail.run.window_end)):
            _fail()
        row = {"news_id": item.news_id, "rank": item.rank, "content_type": metrics.content_type,
               **{key: getattr(metrics, key) for key in (*_COUNTS, "ctr")}, "hot_score": item.hot_score.score}
        if include_baseline:
            row["baseline"] = _baseline(item.baseline, item.news_id, metrics.content_type)
        rows.append(row)
    dataset = {"run_id": str(UUID(run_id)), "window_start": detail.run.window_start.isoformat(),
               "window_end": detail.run.window_end.isoformat(),
               "bundle_version": detail.run.production_bundle_version, "rows": rows}
    if extended:
        workflow = detail.run.workflow_version
        if not isinstance(workflow, str) or not workflow.strip() or len(workflow) > 128:
            _fail()
        dataset["provenance"] = {"workflow_version": workflow,
                                 "selection_scope_sha256": selection_scope(detail, tenant_id=tenant_id)}
    return dataset
