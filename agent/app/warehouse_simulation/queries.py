"""Fixed read-only SQL examples over the standalone v8 aggregate warehouse.

These queries produce candidates and evidence, never the Agent's authoritative
hot score. No arbitrary SQL, enterprise endpoint, raw user data, or full article
body is exposed. A business hour uses the latest revision even when it is pending.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.warehouse_simulation.real_news_catalog import CATALOG_SHA256, load_catalog

TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DATASET = Path(__file__).resolve().parents[3] / "output/warehouse/real-news-v8-20261014"
TENANTS = ("11111111-1111-4111-8111-111111111111", "33333333-3333-4333-8333-333333333333")
DEFAULT_NEWS_ID = "20260903A05YQE00"
DATASET_SHA256 = "a933780d744f0e95447f46cc328a317993987c61e870a36fe97b89485463fc81"
SCHEMA_SHA256 = "5e08cf08c090796b7aee62be3a8dcb4f688452d29fc3567d30f6f2c3465ed073"
QUERY_NAMES = ("ranking", "hourly", "five_minute", "channel", "daily", "baseline", "windows")
SORT_METRICS = ("clicks", "ctr", "detail_page_views", "detail_visitors", "interactions")
TABLES = ("dim_behavior_policy", "dim_channel", "dim_news", "window_snapshot", "news_metric_5min",
          "news_metric_channel_hourly", "news_metric_hourly", "news_metric_daily",
          "news_metric_baseline_hourly", "content_change_event", "content_tombstone", "index_cleanup_task")
QUERY_CATALOG_VERSION = "v8-readonly-query-catalog-v1"

GATE_SQL = """SELECT tenant_id,event_time,snapshot_revision,window_end,watermark_at,snapshot_at,
 completeness,quality_status,policy_version
FROM news_sim_v8.window_snapshot
WHERE tenant_id=%(tenant_id)s::uuid AND event_time=%(hour)s::timestamptz
ORDER BY snapshot_revision DESC LIMIT 1;"""
DAILY_GATE_SQL = """SELECT count(*) AS observed_windows,
 count(*) FILTER (WHERE completeness IN ('complete','corrected') AND quality_status='passed'
   AND watermark_at>=window_end) AS passed_windows,
 count(*) FILTER (WHERE completeness NOT IN ('complete','corrected') OR quality_status<>'passed'
   OR watermark_at<window_end) AS excluded_windows,
 min(event_time) AS earliest_window,max(event_time) AS latest_window
FROM news_sim_v8.latest_window_snapshot
WHERE tenant_id=%(tenant_id)s::uuid
 AND event_time >= %(start_date)s::date::timestamp AT TIME ZONE 'Asia/Shanghai'
 AND event_time < (%(end_date)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai';"""
SCHEMA_COLUMNS_SQL = """SELECT table_name,column_name,ordinal_position
FROM information_schema.columns
WHERE table_schema='news_sim_v8' AND table_name=ANY(%(table_names)s)
ORDER BY table_name,ordinal_position;"""
DATASET_WINDOW_SQL = """SELECT min(event_time) AS window_start,max(event_time) AS open_window_start
FROM news_sim_v8.window_snapshot WHERE tenant_id=%(tenant_id)s::uuid;"""
_LATEST = """WITH latest AS (
 SELECT * FROM news_sim_v8.window_snapshot
 WHERE tenant_id=%(tenant_id)s::uuid AND event_time=%(hour)s::timestamptz
 ORDER BY snapshot_revision DESC LIMIT 1
)
"""
_QUALIFIED = """w.completeness IN ('complete','corrected') AND w.quality_status='passed'
 AND w.watermark_at>=w.window_end"""
_CONTENT = "n.tenant_id,n.news_id,n.title,n.category,n.source_url,n.publish_time,n.content_sha256"
_METRICS = """m.impressions,m.clicks,m.unique_users,m.detail_page_views,m.direct_page_views,
 m.detail_visitors,m.total_duration_seconds,m.effective_consumptions,m.interactions,
 m.likes,m.comments,m.shares,m.favorites,m.negative_feedback,
 m.clicks::numeric/NULLIF(m.impressions,0) AS ctr"""
_HOUR_JOIN = """FROM news_sim_v8.news_metric_hourly m
JOIN latest w ON w.tenant_id=m.tenant_id AND w.event_time=m.event_time
 AND w.snapshot_revision=m.snapshot_revision
JOIN news_sim_v8.eligible_news n ON n.tenant_id=m.tenant_id AND n.news_id=m.news_id
"""
SQL_QUERIES = {
    "ranking": _LATEST + f"""SELECT {_CONTENT},m.event_time,m.snapshot_revision,{_METRICS},
 count(*) OVER () AS matched_rows
{_HOUR_JOIN}WHERE {_QUALIFIED}
 AND (%(sort_metric)s <> 'ctr' OR m.impressions >= %(min_impressions)s)
ORDER BY __ORDER_METRIC__ DESC NULLS LAST,n.news_id ASC LIMIT %(limit)s;""",
    "hourly": _LATEST + f"""SELECT {_CONTENT},m.event_time,m.snapshot_revision,{_METRICS},
 m.uv_method,w.watermark_at,w.snapshot_at,w.completeness,w.quality_status,
 m.total_duration_seconds::numeric/NULLIF(m.detail_page_views,0) AS average_duration_per_detail_view
{_HOUR_JOIN}WHERE {_QUALIFIED} AND n.news_id=%(news_id)s LIMIT 1;""",
    "five_minute": _LATEST + f"""SELECT {_CONTENT},m.event_time,m.snapshot_revision,{_METRICS},
 GREATEST(m.event_time,n.publish_time) AS coverage_start,
 m.event_time+interval '5 minutes' AS coverage_end,
 (n.publish_time>m.event_time) AS publication_partial_bucket
FROM news_sim_v8.news_metric_5min m
JOIN latest w ON w.tenant_id=m.tenant_id AND m.event_time>=w.event_time
 AND m.event_time<w.window_end AND m.snapshot_revision=w.snapshot_revision
JOIN news_sim_v8.eligible_news n ON n.tenant_id=m.tenant_id AND n.news_id=m.news_id
WHERE {_QUALIFIED} AND n.news_id=%(news_id)s
 AND m.event_time+interval '5 minutes'>n.publish_time
ORDER BY m.event_time ASC LIMIT 12;""",
    "channel": _LATEST + f"""SELECT {_CONTENT},m.event_time,m.snapshot_revision,m.channel_id,
 c.channel_name,c.platform,{_METRICS}
FROM news_sim_v8.news_metric_channel_hourly m
JOIN latest w ON w.tenant_id=m.tenant_id AND w.event_time=m.event_time
 AND m.snapshot_revision=w.snapshot_revision
JOIN news_sim_v8.eligible_news n ON n.tenant_id=m.tenant_id AND n.news_id=m.news_id
JOIN news_sim_v8.dim_channel c ON c.tenant_id=m.tenant_id AND c.channel_id=m.channel_id
WHERE {_QUALIFIED} AND n.news_id=%(news_id)s ORDER BY m.channel_id ASC LIMIT 6;""",
    "daily": f"""SELECT {_CONTENT},m.stat_date,{_METRICS},m.coverage_start,m.coverage_end,
 count(*) OVER () AS matched_rows,
 m.observed_hours,m.excluded_hours,m.completeness,m.snapshot_revision,m.uv_method
FROM news_sim_v8.news_metric_daily m
JOIN news_sim_v8.eligible_news n ON n.tenant_id=m.tenant_id AND n.news_id=m.news_id
WHERE m.tenant_id=%(tenant_id)s::uuid AND n.news_id=%(news_id)s
 AND m.stat_date BETWEEN %(start_date)s::date AND %(end_date)s::date
ORDER BY m.stat_date ASC LIMIT %(limit)s;""",
    "baseline": _LATEST + f"""SELECT {_CONTENT},m.event_time,m.snapshot_revision,{_METRICS},
 b.sample_count,b.baseline_version,b.baseline_impressions,b.baseline_clicks,
 b.baseline_effective_consumptions,b.baseline_interactions,
 CASE WHEN b.sample_count IS NULL OR b.sample_count=0 THEN 'missing_history'
      WHEN b.baseline_clicks=0 THEN 'zero_baseline' ELSE 'comparable' END AS baseline_status,
 CASE WHEN b.sample_count>0 THEN m.clicks-b.baseline_clicks END AS clicks_delta,
 CASE WHEN b.sample_count>0 THEN (m.clicks-b.baseline_clicks)/NULLIF(b.baseline_clicks,0) END AS clicks_growth_rate
{_HOUR_JOIN}LEFT JOIN news_sim_v8.news_metric_baseline_hourly b
 ON b.tenant_id=m.tenant_id AND b.news_id=m.news_id AND b.event_time=m.event_time
WHERE {_QUALIFIED} AND n.news_id=%(news_id)s LIMIT 1;""",
    "windows": """SELECT tenant_id,event_time,snapshot_revision,window_end,watermark_at,snapshot_at,
 completeness,quality_status,policy_version,
 (snapshot_revision=max(snapshot_revision) OVER ()) AS is_latest,
 count(*) OVER () AS matched_rows,
 TRUE AS diagnostic_only
FROM news_sim_v8.window_snapshot
WHERE tenant_id=%(tenant_id)s::uuid AND event_time=%(hour)s::timestamptz
ORDER BY snapshot_revision DESC LIMIT 4;""",
}


class WarehouseQueryError(ValueError):
    pass


class WarehouseWindowUnavailable(WarehouseQueryError):
    """A pending/missing latest revision is a failure, never an empty ranking."""


@dataclass(frozen=True)
class QueryRequest:
    query: str = "ranking"
    tenant_id: str = TENANTS[0]
    hour: str | datetime | None = None
    news_id: str = DEFAULT_NEWS_ID
    sort: str = "clicks"
    limit: int = 20
    min_impressions: int = 1000
    start_date: str | date | None = None
    end_date: str | date | None = None


@dataclass(frozen=True)
class CompiledQuery:
    query_name: str
    sql: str
    parameters: dict[str, Any]
    gate_sql: str
    gate_parameters: dict[str, Any]
    row_limit: int
    diagnostic_only: bool
    identity: dict[str, Any]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WarehouseQueryError("duplicate manifest JSON key")
        result[key] = value
    return result


def _read_json(path: Path, maximum: int) -> dict[str, Any]:
    if path.is_symlink() or path.suffix != ".json":
        raise WarehouseQueryError("expected an explicit regular JSON artifact")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if not raw or len(raw) > maximum:
        raise WarehouseQueryError("query artifact exceeds its size budget")
    try:
        result = json.loads(raw, object_pairs_hook=_strict_object)
    except (ValueError, UnicodeError) as exc:
        raise WarehouseQueryError("invalid query artifact JSON") from exc
    if not isinstance(result, dict):
        raise WarehouseQueryError("query artifact must be an object")
    return result


def _time(value: Any) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise WarehouseQueryError("hour must be an ISO timestamp with a timezone") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise WarehouseQueryError("hour must include a timezone")
    return result.astimezone(TZ)


def _date(value: Any) -> date:
    try:
        if type(value) is date:
            return value
        if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            raise ValueError()
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise WarehouseQueryError("date must be YYYY-MM-DD") from exc


def load_identity(dataset_dir: Path | str = DEFAULT_DATASET) -> tuple[dict[str, Any], dict[str, Any]]:
    directory = Path(dataset_dir).absolute()
    manifest_path = directory / "manifest.json"
    manifest = _read_json(manifest_path, 1024 * 1024)
    unsigned = dict(manifest)
    digest = unsigned.pop("dataset_sha256", None)
    if digest != DATASET_SHA256 or sha256(_json(unsigned).encode()).hexdigest() != DATASET_SHA256:
        raise WarehouseQueryError("dataset manifest differs from the fixed v8 query identity")
    if (manifest.get("schema") != "news_sim_v8" or manifest.get("tenants") != list(TENANTS)
            or manifest.get("catalog_sha256") != CATALOG_SHA256
            or manifest.get("schema_sha256") != SCHEMA_SHA256
            or manifest.get("news_content_synthetic") is not False
            or manifest.get("metric_data_synthetic") is not True
            or set(manifest.get("tables", {})) != set(TABLES)):
        raise WarehouseQueryError("query manifest schema, tenant or content provenance mismatch")
    schema_path = directory / "schema.sql"
    if schema_path.is_symlink() or schema_path.stat().st_size > 128 * 1024 or sha256(schema_path.read_bytes()).hexdigest() != SCHEMA_SHA256:
        raise WarehouseQueryError("v8 frozen schema digest mismatch")
    load_catalog(directory / "catalog.json")
    columns = {table: manifest["tables"][table]["columns"] for table in TABLES}
    if any(not isinstance(values, list) or not values or len(set(values)) != len(values)
           or any(not isinstance(field, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", field) for field in values)
           for values in columns.values()):
        raise WarehouseQueryError("invalid frozen table column directory")
    identity = {
        "dataset_version": manifest["dataset_version"], "dataset_sha256": DATASET_SHA256,
        "manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
        "catalog_sha256": CATALOG_SHA256, "schema_sha256": SCHEMA_SHA256,
        "table_columns_sha256": sha256(_json(columns).encode()).hexdigest(),
        "query_catalog_version": QUERY_CATALOG_VERSION,
        "query_catalog_sha256": sha256(_json({"queries": SQL_QUERIES, "gate": GATE_SQL,
                                                "daily_gate": DAILY_GATE_SQL, "columns": SCHEMA_COLUMNS_SQL}).encode()).hexdigest(),
    }
    return manifest, identity


def compile_query(request: QueryRequest, dataset_dir: Path | str = DEFAULT_DATASET) -> CompiledQuery:
    manifest, identity = load_identity(dataset_dir)
    if request.query not in QUERY_NAMES or request.sort not in SORT_METRICS:
        raise WarehouseQueryError("query and sort must be fixed allowlisted choices")
    if request.tenant_id not in TENANTS:
        raise WarehouseQueryError("only the two isolated v8 fixture tenants are allowed")
    if type(request.limit) is not int or not 1 <= request.limit <= 100:
        raise WarehouseQueryError("limit must be an integer in 1..100")
    if type(request.min_impressions) is not int or not 0 <= request.min_impressions <= 10**9:
        raise WarehouseQueryError("minimum impressions must be an integer in 0..1000000000")
    if not isinstance(request.news_id, str) or not re.fullmatch(r"[0-9]{8}[A-Za-z0-9]{4,32}", request.news_id):
        raise WarehouseQueryError("invalid real-news ID")
    if request.query not in {"ranking", "windows"} and request.news_id not in {
            row["news_id"] for row in load_catalog(Path(dataset_dir) / "catalog.json")}:
        raise WarehouseQueryError("news ID is absent from the independently frozen real-news catalog")
    start, end = _time(manifest["closed_window_start"]), _time(manifest["closed_window_end"])
    parameters: dict[str, Any] = {"tenant_id": request.tenant_id}
    if request.query == "daily":
        if request.hour is not None:
            raise WarehouseQueryError("daily uses dates, not an hourly window")
        last = _date(request.end_date) if request.end_date is not None else end.date()
        first = _date(request.start_date) if request.start_date is not None else max(start.date(), last - timedelta(days=6))
        if not start.date() <= first <= last <= end.date() or (last - first).days >= 31:
            raise WarehouseQueryError("daily dates must be inside fixture coverage and span at most 31 days")
        bound = min(request.limit, 31)
        parameters.update(news_id=request.news_id, start_date=first, end_date=last, limit=bound)
        gate_sql = DAILY_GATE_SQL
        gate_parameters = {field: parameters[field] for field in ("tenant_id", "start_date", "end_date")}
    else:
        if request.start_date is not None or request.end_date is not None:
            raise WarehouseQueryError("date ranges are only supported by the daily query")
        hour = _time(request.hour) if request.hour is not None else end - timedelta(hours=1)
        if hour.minute or hour.second or hour.microsecond or not start <= hour <= end:
            raise WarehouseQueryError("hour must be one complete aligned hour inside fixture coverage")
        parameters["hour"] = hour
        bound = {"ranking": request.limit, "hourly": 1, "five_minute": 12, "channel": 6,
                 "baseline": 1, "windows": 4}[request.query]
        if request.query == "ranking":
            parameters.update(sort_metric=request.sort, min_impressions=request.min_impressions, limit=bound)
        elif request.query != "windows":
            parameters["news_id"] = request.news_id
        gate_sql = GATE_SQL
        gate_parameters = {field: parameters[field] for field in ("tenant_id", "hour")}
    sql = SQL_QUERIES[request.query]
    # Identifier substitution only uses these literal entries; user text never
    # becomes an identifier. Every data value remains a driver-bound parameter.
    if request.query == "ranking":
        order = "ctr" if request.sort == "ctr" else f"m.{request.sort}"
        sql = sql.replace("__ORDER_METRIC__", order)
    return CompiledQuery(request.query, sql, parameters, gate_sql, gate_parameters, bound,
                         request.query == "windows", identity)


def _json_friendly(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_friendly(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_friendly(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    return value


def _validate_gate(gate: Any, compiled: CompiledQuery) -> None:
    if not isinstance(gate, dict):
        raise WarehouseWindowUnavailable("requested latest warehouse window does not exist")
    if compiled.query_name == "daily":
        if not gate.get("observed_windows"):
            raise WarehouseWindowUnavailable("daily range has no observed warehouse windows")
        return  # Partial daily coverage is intentionally shown, with excluded hours.
    if (str(gate.get("tenant_id")) != compiled.parameters["tenant_id"]
            or _time(gate.get("event_time")) != compiled.parameters["hour"]):
        raise WarehouseQueryError("snapshot gate crossed tenant or hour identity")
    if compiled.diagnostic_only:
        return
    if (gate.get("completeness") not in {"complete", "corrected"} or gate.get("quality_status") != "passed"
            or _time(gate.get("watermark_at")) < _time(gate.get("window_end"))):
        raise WarehouseWindowUnavailable("latest snapshot is pending/incomplete; business query blocked without falling back to an older revision")


def _validate_schema_columns(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    observed = {table: [] for table in TABLES}
    for row in rows:
        name = row.get("table_name")
        if name not in observed:
            raise WarehouseQueryError("database returned a foreign table schema")
        if row.get("ordinal_position") != len(observed[name]) + 1:
            raise WarehouseQueryError("database table column ordinal mismatch")
        observed[name].append(row.get("column_name"))
    if observed != {table: manifest["tables"][table]["columns"] for table in TABLES}:
        raise WarehouseQueryError("database table columns differ from the frozen v8 directory")


def run_query(request: QueryRequest, dataset_dir: Path | str = DEFAULT_DATASET, *, connect=None) -> dict[str, Any]:
    compiled = compile_query(request, dataset_dir)
    manifest, _ = load_identity(dataset_dir)
    catalog = {row["news_id"]: row for row in load_catalog(Path(dataset_dir) / "catalog.json")}
    if request.query not in {"ranking", "windows"} and request.news_id not in catalog:
        raise WarehouseQueryError("news ID is absent from the independently frozen real-news catalog")
    if connect is None:
        import psycopg
        from psycopg.rows import dict_row
        def connect(**kwargs):
            return psycopg.connect(row_factory=dict_row, **kwargs)
    connection_arguments = {"host": "127.0.0.1", "port": 28438, "dbname": "news_demo", "user": "news_demo",
                            "password": "local-v8-only", "connect_timeout": 5, "autocommit": True}
    with connect(**connection_arguments) as connection:
        with connection.cursor() as cursor:
            try:
                cursor.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;")
                cursor.execute("SET LOCAL TIME ZONE 'Asia/Shanghai';")
                cursor.execute("SET LOCAL statement_timeout = '10s';")
                cursor.execute("SET LOCAL lock_timeout = '2s';")
                cursor.execute(SCHEMA_COLUMNS_SQL, {"table_names": list(TABLES)})
                column_rows = cursor.fetchmany(513)
                if len(column_rows) > 512:
                    raise WarehouseQueryError("database column response exceeded its bound")
                _validate_schema_columns(column_rows, manifest)
                cursor.execute(DATASET_WINDOW_SQL, {"tenant_id": request.tenant_id})
                bounds = cursor.fetchone()
                if (not isinstance(bounds, dict) or _time(bounds.get("window_start")) != _time(manifest["closed_window_start"])
                        or _time(bounds.get("open_window_start")) != _time(manifest["open_window_start"])):
                    raise WarehouseQueryError("database coverage differs from the fixed dataset identity")
                cursor.execute(compiled.gate_sql, compiled.gate_parameters)
                gate = cursor.fetchone()
                _validate_gate(gate, compiled)
                cursor.execute(compiled.sql, compiled.parameters)
                rows = cursor.fetchmany(compiled.row_limit + 1)
                if len(rows) > compiled.row_limit:
                    raise WarehouseQueryError("SQL result exceeded the fixed query bound")
                for row in rows:
                    if compiled.diagnostic_only:
                        if str(row.get("tenant_id")) != request.tenant_id or row.get("diagnostic_only") is not True:
                            raise WarehouseQueryError("diagnostic result crossed tenant scope")
                        continue
                    original = catalog.get(row.get("news_id"))
                    if str(row.get("tenant_id")) != request.tenant_id:
                        raise WarehouseQueryError("query result crossed tenant scope")
                    if original is None or any(row.get(field) != original[source]
                        for field, source in (("title", "title"), ("source_url", "url"), ("content_sha256", "content_sha256"))):
                        raise WarehouseQueryError("query result differs from frozen real-news content identity")
                    if _time(row.get("publish_time")) != _time(original["publish_time"]):
                        raise WarehouseQueryError("query result changed original publication time")
                    if compiled.query_name not in {"daily"} and row.get("snapshot_revision") != gate.get("snapshot_revision"):
                        raise WarehouseQueryError("query result did not use the latest qualified revision")
                cursor.execute("COMMIT;")
            except Exception:
                cursor.execute("ROLLBACK;")
                raise
    matched_rows = rows[0].get("matched_rows", len(rows)) if rows else 0
    if type(matched_rows) is not int or matched_rows < len(rows):
        raise WarehouseQueryError("invalid bounded-result match count")
    return {
        "status": "passed", "label": "真实新闻 · 模拟指标 · 查询候选，非权威热度",
        "query": compiled.query_name, "diagnostic_only": compiled.diagnostic_only,
        "tenant_id": request.tenant_id, "identity": compiled.identity,
        "parameters": _json_friendly(compiled.parameters), "gate": _json_friendly(gate),
        "transaction": {"isolation": "repeatable_read", "read_only": True,
                        "statement_timeout_ms": 10000, "lock_timeout_ms": 2000},
        "row_limit": compiled.row_limit, "row_count": len(rows), "rows": _json_friendly(rows),
        "matched_rows": matched_rows, "truncated": matched_rows > len(rows),
        "omitted_rows": matched_rows - len(rows),
        "sql_sha256": sha256(compiled.sql.encode()).hexdigest(),
        "news_content_synthetic": False, "metric_data_synthetic": True,
        "daily_coverage_policy": "stored per-news daily UV; partial and excluded hours remain visible; UV never summed",
        "integration_status": "standalone_query_examples_not_connected_to_agent",
    }
