"""Bounded query semantics and same-snapshot business gates for standalone v8."""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import sys

import pytest

from simulation.warehouse_simulation import queries as q
from simulation.warehouse_simulation.real_news_catalog import load_catalog


@pytest.fixture(scope="module")
def manifest():
    return q.load_identity()[0]


@pytest.fixture
def gate():
    hour = datetime.fromisoformat("2026-10-14T19:00:00+08:00")
    return {"tenant_id": q.TENANTS[0], "event_time": hour, "snapshot_revision": 2,
            "window_end": hour + timedelta(hours=1), "watermark_at": hour + timedelta(hours=1, minutes=10),
            "snapshot_at": hour + timedelta(hours=1, minutes=15), "completeness": "corrected",
            "quality_status": "passed", "policy_version": "sim-click-attributed-reading-v8.1"}


@pytest.fixture
def article_row():
    original = next(row for row in load_catalog() if row["news_id"] == q.DEFAULT_NEWS_ID)
    return {"tenant_id": q.TENANTS[0], "news_id": original["news_id"], "title": original["title"],
            "source_url": original["url"], "publish_time": datetime.fromisoformat(original["publish_time"]),
            "content_sha256": original["content_sha256"], "snapshot_revision": 2,
            "impressions": 0, "clicks": 0, "ctr": None,
            "baseline_clicks": None, "sample_count": 0, "clicks_growth_rate": None}


class FakeCursor:
    def __init__(self, manifest, gate, rows):
        self.manifest, self.gate, self.rows = manifest, gate, rows
        self.calls = []
        self.columns = [{"table_name": table, "column_name": field, "ordinal_position": position}
                        for table in sorted(q.TABLES)
                        for position, field in enumerate(manifest["tables"][table]["columns"], 1)]

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        pass

    def execute(self, sql, parameters=None):
        self.calls.append((sql, deepcopy(parameters)))

    def fetchmany(self, maximum):
        sql = self.calls[-1][0]
        rows = self.columns if sql == q.SCHEMA_COLUMNS_SQL else self.rows
        return rows[:maximum]

    def fetchone(self):
        sql = self.calls[-1][0]
        if sql == q.DATASET_WINDOW_SQL:
            return {"window_start": datetime.fromisoformat(self.manifest["closed_window_start"]),
                    "open_window_start": datetime.fromisoformat(self.manifest["open_window_start"])}
        return self.gate


class FakeConnection:
    def __init__(self, cursor):
        self.saved_cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        pass

    def cursor(self):
        return self.saved_cursor


def _connect(cursor):
    def connect(**parameters):
        assert parameters == {"host": "127.0.0.1", "port": 28438, "dbname": "news_demo",
                              "user": "news_demo", "password": "local-v8-only", "connect_timeout": 5,
                              "autocommit": True}
        return FakeConnection(cursor)
    return connect


def test_exact_query_names_bounds_and_every_identity_hash():
    assert q.QUERY_NAMES == ("ranking", "hourly", "five_minute", "channel", "daily", "baseline", "windows")
    for name in q.QUERY_NAMES:
        compiled = q.compile_query(q.QueryRequest(query=name))
        assert compiled.row_limit <= {"ranking": 100, "hourly": 1, "five_minute": 12, "channel": 6,
                                      "daily": 31, "baseline": 1, "windows": 4}[name]
        assert all(len(compiled.identity[field]) == 64 for field in (
            "dataset_sha256", "manifest_sha256", "catalog_sha256", "schema_sha256",
            "table_columns_sha256", "query_catalog_sha256"))
        assert "content AS" not in compiled.sql and "n.content," not in compiled.sql


@pytest.mark.parametrize("changes", [
    {"query": "DELETE"}, {"sort": "clicks; DROP SCHEMA news_sim_v8"}, {"tenant_id": "foreign"},
    {"limit": 0}, {"limit": 101}, {"limit": True}, {"min_impressions": -1},
    {"min_impressions": True}, {"min_impressions": 10**9 + 1},
    {"hour": "2026-10-14T19:00:00"}, {"hour": "2026-10-14T19:01:00+08:00"},
    {"hour": "2026-10-14T19:00:01+08:00"}, {"hour": "2026-10-14T19:00:00.001+08:00"},
    {"hour": "2026-10-15T00:00:00+08:00"}, {"hour": "2026-09-29T23:00:00+08:00"},
    {"news_id": "x' OR 1=1 --"}, {"query": "hourly", "news_id": "20000101A0000000"},
    {"start_date": "2026-10-01"}, {"query": "daily", "hour": "2026-10-14T19:00:00+08:00"},
    {"query": "daily", "start_date": "2026-09-29"},
    {"query": "daily", "start_date": "2026-10-14", "end_date": "2026-10-01"},
])
def test_invalid_parameters_are_rejected_without_a_database_call(changes):
    with pytest.raises(q.WarehouseQueryError):
        q.compile_query(q.QueryRequest(**changes))


def test_utc_hour_normalizes_and_sort_only_uses_fixed_identifiers():
    for metric in q.SORT_METRICS:
        compiled = q.compile_query(q.QueryRequest(hour="2026-10-14T11:00:00+00:00", sort=metric))
        assert compiled.parameters["hour"].isoformat() == "2026-10-14T19:00:00+08:00"
        assert "DESC NULLS LAST,n.news_id ASC" in compiled.sql
        assert "%(tenant_id)s" in compiled.sql and "%(min_impressions)s" in compiled.sql
        assert "%(hour)s" in compiled.sql
        assert q.TENANTS[0] not in compiled.sql
        assert "impressions >= %(min_impressions)s" in compiled.sql


def test_hourly_channel_and_bucket_queries_join_the_latest_full_revision():
    for name in ("ranking", "hourly", "five_minute", "channel", "baseline"):
        sql = q.compile_query(q.QueryRequest(query=name)).sql
        assert "ORDER BY snapshot_revision DESC LIMIT 1" in sql
        assert "w.tenant_id=m.tenant_id" in sql
        assert "snapshot_revision=m.snapshot_revision" in sql or "m.snapshot_revision=w.snapshot_revision" in sql
        assert "JOIN news_sim_v8.eligible_news n ON n.tenant_id=m.tenant_id AND n.news_id=m.news_id" in sql
        assert "w.quality_status='passed'" in sql
    bucket = q.SQL_QUERIES["five_minute"]
    assert "m.event_time>=w.event_time" in bucket and "m.event_time<w.window_end" in bucket
    assert "m.event_time+interval '5 minutes'>n.publish_time" in bucket
    assert "GREATEST(m.event_time,n.publish_time)" in bucket


def test_null_zero_denominators_and_daily_uv_are_not_conflated():
    assert all("NULLIF(m.impressions,0)" in q.SQL_QUERIES[name]
               for name in ("ranking", "hourly", "five_minute", "channel", "daily", "baseline"))
    baseline = q.SQL_QUERIES["baseline"]
    assert "b.sample_count>0" in baseline and "NULLIF(b.baseline_clicks,0)" in baseline
    assert all(status in baseline for status in ("missing_history", "zero_baseline", "comparable"))
    assert "COALESCE" not in baseline
    daily = q.SQL_QUERIES["daily"]
    assert "m.unique_users" in daily and "m.detail_visitors" in daily
    assert "SUM(" not in daily.upper()
    assert "m.excluded_hours,m.completeness" in daily


def test_repeatable_read_readonly_gate_and_results_share_the_same_transaction(manifest, gate, article_row):
    cursor = FakeCursor(manifest, gate, [article_row])
    result = q.run_query(q.QueryRequest(query="baseline"), connect=_connect(cursor))
    calls = [sql for sql, _ in cursor.calls]
    assert calls[:4] == ["BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;", "SET LOCAL TIME ZONE 'Asia/Shanghai';",
                         "SET LOCAL statement_timeout = '10s';", "SET LOCAL lock_timeout = '2s';"]
    assert calls.index(q.GATE_SQL) < len(calls) - 2
    assert calls[-1] == "COMMIT;" and "ROLLBACK;" not in calls
    assert result["rows"][0]["ctr"] is None
    assert result["rows"][0]["baseline_clicks"] is None
    assert result["transaction"]["read_only"] is True
    assert result["integration_status"] == "standalone_query_examples_not_connected_to_agent"


@pytest.mark.parametrize("changes", [
    {"quality_status": "pending"}, {"completeness": "incomplete"},
    {"watermark_at": datetime.fromisoformat("2026-10-14T19:59:00+08:00")},
])
def test_pending_latest_revision_raises_and_never_queries_older_or_empty_rank(manifest, gate, changes):
    gate.update(changes)
    cursor = FakeCursor(manifest, gate, [])
    with pytest.raises(q.WarehouseWindowUnavailable, match="without falling back"):
        q.run_query(q.QueryRequest(), connect=_connect(cursor))
    assert cursor.calls[-1][0] == "ROLLBACK;"
    assert all("ORDER BY m.clicks" not in sql for sql, _ in cursor.calls)


def test_windows_diagnostic_can_show_pending_without_becoming_business_result(manifest, gate):
    gate.update(quality_status="pending", completeness="incomplete")
    rows = [{**gate, "diagnostic_only": True, "is_latest": True}]
    cursor = FakeCursor(manifest, gate, rows)
    result = q.run_query(q.QueryRequest(query="windows"), connect=_connect(cursor))
    assert result["diagnostic_only"] is True and result["rows"][0]["quality_status"] == "pending"


def test_foreign_gate_or_result_tenant_is_rejected(manifest, gate, article_row):
    foreign = {**gate, "tenant_id": q.TENANTS[1]}
    with pytest.raises(q.WarehouseQueryError, match="crossed tenant"):
        q.run_query(q.QueryRequest(query="hourly"), connect=_connect(FakeCursor(manifest, foreign, [article_row])))
    row = {**article_row, "tenant_id": q.TENANTS[1]}
    with pytest.raises(q.WarehouseQueryError, match="crossed tenant"):
        q.run_query(q.QueryRequest(query="hourly"), connect=_connect(FakeCursor(manifest, gate, [row])))


def test_old_revision_in_current_result_is_rejected(manifest, gate, article_row):
    row = {**article_row, "snapshot_revision": 1}
    with pytest.raises(q.WarehouseQueryError, match="latest qualified revision"):
        q.run_query(q.QueryRequest(query="five_minute"), connect=_connect(FakeCursor(manifest, gate, [row])))


def test_live_column_drift_and_bounded_result_fail_closed(manifest, gate, article_row):
    cursor = FakeCursor(manifest, gate, [article_row])
    cursor.columns[0]["column_name"] = "foreign_field"
    with pytest.raises(q.WarehouseQueryError, match="columns differ"):
        q.run_query(q.QueryRequest(), connect=_connect(cursor))
    cursor = FakeCursor(manifest, gate, [article_row, article_row])
    with pytest.raises(q.WarehouseQueryError, match="result exceeded"):
        q.run_query(q.QueryRequest(limit=1), connect=_connect(cursor))


def test_daily_excluded_windows_and_partial_day_remain_visible(manifest, article_row):
    gate = {"observed_windows": 168, "passed_windows": 166, "excluded_windows": 2}
    row = {**article_row, "stat_date": "2026-10-14", "unique_users": 19, "detail_visitors": 27,
           "completeness": "partial", "excluded_hours": 1, "observed_hours": 19}
    result = q.run_query(q.QueryRequest(query="daily", start_date="2026-10-08", end_date="2026-10-14", limit=100),
                         connect=_connect(FakeCursor(manifest, gate, [row])))
    assert result["row_limit"] == 31 and result["gate"]["excluded_windows"] == 2
    assert result["rows"][0]["completeness"] == "partial" and result["rows"][0]["detail_visitors"] == 27


def test_daily_limit_discloses_omitted_history_instead_of_claiming_full_range(manifest, article_row):
    gate = {"observed_windows": 168, "passed_windows": 166, "excluded_windows": 2}
    row = {**article_row, "stat_date": "2026-10-08", "matched_rows": 7}
    result = q.run_query(q.QueryRequest(query="daily", limit=1),
                         connect=_connect(FakeCursor(manifest, gate, [row])))
    assert result["row_count"] == 1 and result["matched_rows"] == 7
    assert result["truncated"] is True and result["omitted_rows"] == 6


def test_cli_show_sql_and_output_refuse_overwrite_without_database(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import query_real_news_warehouse as cli
    monkeypatch.setattr(cli, "run_query", lambda *a, **kw: pytest.fail("show-sql connected to database"))
    output = tmp_path / "new-query"
    assert cli.main(["--query", "ranking", "--sort", "ctr", "--show-sql", "--output", str(output)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["executed"] is False and shown["parameters"]["min_impressions"] == 1000
    assert {path.name for path in output.iterdir()} == {"query.sql", "gate.sql", "parameters.json", "snapshot.json"}
    with pytest.raises(SystemExit) as failure:
        cli.main(["--show-sql", "--output", str(output)])
    assert failure.value.code == 2


@pytest.mark.skipif(os.environ.get("NEWSAGENT_RUN_V8_QUERY_INTEGRATION") != "1", reason="opt-in local readonly PostgreSQL")
def test_local_postgres_all_queries_null_baseline_and_pending_gate():
    for name in q.QUERY_NAMES:
        result = q.run_query(q.QueryRequest(query=name, limit=3))
        assert result["status"] == "passed" and result["row_count"] <= result["row_limit"]
    for hour in ("2026-10-14T18:00:00+08:00", "2026-10-14T20:00:00+08:00"):
        with pytest.raises(q.WarehouseWindowUnavailable):
            q.run_query(q.QueryRequest(hour=hour))
    diagnostic = q.run_query(q.QueryRequest(query="windows", hour="2026-10-14T18:00:00+08:00"))
    assert diagnostic["rows"][0]["snapshot_revision"] == 2
    assert diagnostic["rows"][0]["quality_status"] == "pending"
    early = q.run_query(q.QueryRequest(query="baseline", hour="2026-09-30T00:00:00+08:00"))
    assert early["rows"][0]["sample_count"] == 0
    assert early["rows"][0]["baseline_clicks"] is None and early["rows"][0]["clicks_growth_rate"] is None
