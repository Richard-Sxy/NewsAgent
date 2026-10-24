"""Validate the proposed aggregate SQL in one rolled-back local PostgreSQL transaction.

Input: explicit AUDIENCE_PROPOSAL_DATABASE_URL and --report. No Settings/.env loading.
Only tiny labelled synthetic fixtures are used; no existing news/profile rows are read.
"""
import argparse
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import sqlglot


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "news_audience_proposal_v1"
TENANT = "11111111-1111-4111-8111-111111111111"
OTHER = "33333333-3333-4333-8333-333333333333"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    ddl_path = ROOT / "agent/docs/sql/news-reader-audience-proposal-v1.sql"
    query_path = ROOT / "agent/docs/sql/news-hotspot-audience-query-example.sql"
    source = ddl_path.read_text()
    # Exact control lines only; preserve the actual DDL rather than rewriting its AST.
    ddl = "\n".join(line for line in source.splitlines()
                    if line.strip().upper() not in {"BEGIN;", "COMMIT;"})
    checks = []
    queries = [q for q in sqlglot.parse(query_path.read_text(), read="postgres")
               if isinstance(q, sqlglot.exp.Query)]
    assert len(queries) == 5
    checks.append("all_five_example_queries_parse_as_postgresql")

    def insert(conn, table, values):
        conn.execute(sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
            sql.Identifier(SCHEMA), sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join(sql.Placeholder() for _ in values)), list(values.values()))

    with psycopg.connect(os.environ["AUDIENCE_PROPOSAL_DATABASE_URL"], row_factory=dict_row,
                         connect_timeout=5) as conn:
        exists = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s) AS present",
                              (SCHEMA,)).fetchone()["present"]
        if exists:
            raise RuntimeError("Proposal schema already exists; refuse to modify it")
        try:
            conn.execute(ddl, prepare=False)
            checks.append("ddl_executes_in_postgresql")
            snapshot = dict(tenant_id=TENANT, snapshot_id="fixture-1",
                window_start="2026-10-23T10:00:00Z", window_end="2026-10-23T11:00:00Z",
                profile_cutoff="2026-10-23T10:00:00Z", knowledge_as_of="2026-10-23T11:00:00Z",
                history_start="2026-09-23T10:00:00Z", source_revision=1,
                source_release_sha256="a" * 64, source_mode="synthetic", taxonomy_version="fixture-v1",
                profile_policy_version="prewindow-exclude-trigger-interest-v1",
                identity_policy_version="fixture-identity-v1", reader_definition="detail-view-distinct-v1",
                count_method="exact_synthetic_distinct", generated_at="2026-10-23T11:01:00Z",
                watermark_at="2026-10-23T11:00:00Z", completeness="complete", quality_status="passed")
            insert(conn, "reader_snapshot", snapshot)
            for topic, name in [("ai", "人工智能"), ("finance", "财经")]:
                insert(conn, "topic_dictionary", dict(taxonomy_version="fixture-v1", topic_id=topic,
                    topic_name=name, parent_topic_id=None, topic_level=1))
            population = dict(tenant_id=TENANT, snapshot_id="fixture-1", news_id="fixture-news",
                lookback_hours=720, content_sha256="b" * 64, reader_count=20, profiled_reader_count=10,
                unknown_reader_count=10, cold_start_reader_count=2, baseline_profiled_count=100,
                status="complete", trigger_article_excluded=True)
            insert(conn, "news_reader_population", population)
            insert(conn, "news_reader_population", dict(population, lookback_hours=24))
            topic_row = dict(tenant_id=TENANT, snapshot_id="fixture-1", news_id="fixture-news",
                lookback_hours=720, topic_id="ai", topic_reader_count=8, baseline_topic_count=20,
                taxonomy_version="fixture-v1")
            insert(conn, "news_reader_topic", topic_row)
            insert(conn, "news_reader_topic", dict(topic_row, topic_id="finance",
                topic_reader_count=2, baseline_topic_count=10))
            rows = conn.execute(f"SELECT * FROM {SCHEMA}.v_news_reader_topic_metrics ORDER BY topic_id").fetchall()
            ai, finance = rows
            assert ai["share_all"] == Decimal("0.4") and ai["share_profiled"] == Decimal("0.8")
            assert ai["baseline_share"] == Decimal("0.2") and ai["lift"] == 4
            checks.append("unknown_readers_remain_in_all_readers_denominator")
            checks.append("lift_uses_profiled_share_and_matching_baseline")
            assert finance["low_sample"] and finance["lift"] is None
            checks.append("low_support_suppresses_lift")
            insert(conn, "news_reader_population", dict(population, news_id="empty-news",
                reader_count=0, profiled_reader_count=0, unknown_reader_count=0,
                cold_start_reader_count=0, baseline_profiled_count=0, status="empty"))
            insert(conn, "news_reader_topic", dict(topic_row, news_id="empty-news",
                topic_reader_count=0, baseline_topic_count=0))
            empty = conn.execute(f"SELECT * FROM {SCHEMA}.v_news_reader_topic_metrics WHERE news_id='empty-news'").fetchone()
            assert all(empty[k] is None for k in ("share_all", "share_profiled", "baseline_share", "lift"))
            checks.append("zero_denominators_remain_null")
            for label, table, values in [
                ("cross_tenant_reference_rejected", "news_reader_population", dict(population, tenant_id=OTHER)),
                ("wrong_taxonomy_rejected", "news_reader_topic", dict(topic_row, lookback_hours=24, taxonomy_version="wrong")),
                ("future_profile_cutoff_rejected", "reader_snapshot", dict(snapshot, snapshot_id="bad-cutoff",
                    profile_cutoff="2026-10-23T11:00:00Z"))]:
                try:
                    with conn.transaction():
                        insert(conn, table, values)
                except psycopg.IntegrityError:
                    checks.append(label)
                else:
                    raise AssertionError(label)
            # Execute the actual two aggregate-query examples with synthetic parameters.
            # SQLAlchemy-style binds are changed to DB-API binds without touching :: casts.
            import re
            def bound_query(index):
                return re.sub(r"(?<!:):([A-Za-z_][A-Za-z_0-9]*)", r"%(\1)s",
                              queries[index].sql(dialect="postgres"))
            params = dict(tenant_id=TENANT, reader_snapshot_id="fixture-1",
                          news_ids=["fixture-news", "empty-news", "missing-news"],
                          lookback_hours=720, topic_top_k=1)
            people = conn.execute(bound_query(2), params).fetchall()
            by_id = {r["news_id"]: r for r in people}
            assert by_id["missing-news"]["profile_status"] == "unavailable"
            assert by_id["missing-news"]["reader_count"] is None
            assert by_id["empty-news"]["profile_status"] == "empty"
            checks.append("example_distinguishes_missing_from_measured_empty")
            top = conn.execute(bound_query(3), params).fetchall()
            assert len([r for r in top if r["news_id"] == "fixture-news"]) == 1
            assert next(r for r in top if r["news_id"] == "fixture-news")["topic_id"] == "ai"
            checks.append("example_top_k_is_per_news_and_does_not_multiply_lookbacks")
        finally:
            conn.rollback()
        assert not conn.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s) AS present",
                                (SCHEMA,)).fetchone()["present"]
        checks.append("transaction_rolled_back_no_schema_or_fixture_persisted")
    report = dict(status="passed", source_mode="tiny_synthetic_contract_fixtures",
                  ddl_sha256=hashlib.sha256(source.encode()).hexdigest(),
                  query_sha256=hashlib.sha256(query_path.read_bytes()).hexdigest(), checks=checks,
                  limitations=["No enterprise connection, data or performance evaluation",
                               "Cross-row publisher validation and SQL Adapter are not implemented",
                               "Existing metric and content queries were parsed, not run by this check"])
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "passed", "checks": len(checks), "report": args.report}))


if __name__ == "__main__":
    main()
