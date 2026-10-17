#!/usr/bin/env python3
"""Run one fixed read-only v8 SQL example; only public local demo credentials."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.warehouse_simulation.queries import (
    DEFAULT_DATASET, DEFAULT_NEWS_ID, QUERY_NAMES, SORT_METRICS, TENANTS,
    QueryRequest, _json_friendly, compile_query, run_query,
)


def save_result(directory: Path, compiled, snapshot: dict) -> None:
    if directory.exists() or directory.is_symlink():
        raise ValueError("query output directory already exists; refusing overwrite")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "query.sql").write_text(compiled.sql + "\n", encoding="utf-8")
    (directory / "gate.sql").write_text(compiled.gate_sql + "\n", encoding="utf-8")
    (directory / "parameters.json").write_text(json.dumps(_json_friendly(compiled.parameters), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--query", choices=QUERY_NAMES, default="ranking")
    parser.add_argument("--tenant", choices=TENANTS, default=TENANTS[0])
    parser.add_argument("--hour", help="One timezone-aware aligned hour, e.g. 2026-10-14T19:00:00+08:00")
    parser.add_argument("--news-id", default=DEFAULT_NEWS_ID)
    parser.add_argument("--sort", choices=SORT_METRICS, default="clicks")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--min-impressions", type=int, default=1000, help="CTR-ranking exposure threshold; only applies when --sort ctr")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--show-sql", action="store_true", help="Print fixed SQL and bound parameters without a database connection")
    parser.add_argument("--output", type=Path, help="New directory for SQL, bound parameters and aggregate query snapshot")
    args = parser.parse_args(argv)
    request = QueryRequest(args.query, args.tenant, args.hour, args.news_id, args.sort, args.limit,
                           args.min_impressions, args.start_date, args.end_date)
    try:
        if args.output and (args.output.exists() or args.output.is_symlink()):
            raise ValueError("query output directory already exists; refusing overwrite")
        compiled = compile_query(request, args.dataset)
        if args.show_sql:
            result = {"query": compiled.query_name, "sql": compiled.sql, "parameters": _json_friendly(compiled.parameters),
                      "gate_sql": compiled.gate_sql, "identity": compiled.identity, "executed": False,
                      "label": "真实新闻 · 模拟指标 · 查询候选，非权威热度"}
        else:
            result = run_query(request, args.dataset)
        if args.output:
            save_result(args.output, compiled, result)
    except Exception as exc:
        parser.exit(2, f"Read-only warehouse query failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
