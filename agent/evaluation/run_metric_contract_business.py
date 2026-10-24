"""Deterministic development replay against real metric-source and SQLite SQL.

This is not model quality or enterprise warehouse validation. No network or
dotenv reads are permitted. Cases, expected business numbers, and approval
blueprint are frozen in the versioned development dataset before execution.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import inspect
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
from time import monotonic


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = Path(__file__).with_name("datasets") / "business_sql" / "metric_contract_business_v1.json"


def install_offline_fence() -> None:
    def audit(event, args):
        if event in {"socket.connect", "socket.connect_ex"}:
            raise RuntimeError("offline evaluation forbids network connections")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            name = Path(os.fsdecode(args[0])).name
            if name == ".env" or name.startswith(".env."):
                raise RuntimeError("offline evaluation forbids dotenv reads")
    sys.addaudithook(audit)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, data) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def identity(dataset_path: Path) -> dict:
    paths = sorted((ROOT / "agent/app").rglob("*.py"))
    paths += [Path(__file__), dataset_path, ROOT / "agent/pyproject.toml"]
    def git(*args):
        return subprocess.check_output(["git", "-C", str(ROOT), *args])
    return {
        "git_head": git("rev-parse", "HEAD").decode().strip(),
        "relevant_tracked_diff_sha256": sha(git("diff", "HEAD", "--", "agent/app", "agent/evaluation", "agent/pyproject.toml")),
        "files": [{"path": str(p.relative_to(ROOT)), "sha256": sha(p.read_bytes())} for p in paths],
        "dependencies": {key: importlib.metadata.version(key) for key in ("pydantic", "sqlglot", "SQLAlchemy")},
        "python": sys.version,
        "platform": platform.platform(),
        "sqlite_version": sqlite3.sqlite_version,
        "deployment_image": None,
    }


class ScriptedGenerator:
    """Return only the preregistered SQL; never receives the oracle or data."""

    def __init__(self, sql: str, case_id: str):
        self.sql, self.case_id, self.calls = sql, case_id, 0

    async def run(self, generation_input):
        from app.model_runtime.result import AgentResult
        from app.schemas.text2sql import Text2SqlPlan
        self.calls += 1
        return AgentResult(Text2SqlPlan(sql=self.sql), f"scripted-{self.case_id}", {"scripted": True}, "")


class SqliteAggregateWarehouse:
    """Execute SQL over synthetic hourly rows; no real warehouse connection."""

    def __init__(self, dataset):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("ATTACH DATABASE ':memory:' AS dw")
        columns = dataset["schema_columns"]
        definitions = ", ".join(f"{key} {'TEXT' if key in {'tenant_id', 'event_time', 'news_id', 'content_type'} else 'INTEGER'}" for key in columns)
        self.connection.execute(f"CREATE TABLE {dataset['table']} ({definitions})")
        self.connection.executemany(
            f"INSERT INTO {dataset['table']} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [tuple(row[key] for key in columns) for row in dataset["rows"]],
        )
        self.connection.commit()
        self.connection.execute("PRAGMA query_only = ON")
        self.calls, self.rows = 0, []

    async def execute(self, query):
        from app.clients.enterprise.sql_warehouse import SqlQueryResult
        self.calls += 1
        params = {key: value.isoformat() if isinstance(value, datetime) else value for key, value in query.params.items()}
        begin = monotonic()
        raw = self.connection.execute(query.sql, params).fetchmany(query.max_rows + 1)
        self.rows = [dict(row) for row in raw]
        return SqlQueryResult(tuple(self.rows[:query.max_rows]), int((monotonic() - begin) * 1000), len(raw) > query.max_rows)


def assess_numbers(case: dict, actual: list[dict]) -> dict:
    gold = case["gold_rows"]
    if gold is None:
        click_gold = case.get("expected_clicks")
        errors = []
        if click_gold is not None:
            clicks = {row["news_id"]: row["clicks"] for row in actual}
            errors = [{"news_id": key, "metric": "clicks", "expected": value, "actual": clicks.get(key)} for key, value in click_gold.items() if clicks.get(key) != value]
        return {"full_numeric_scored": False, "reason": case["numeric_not_scored_reason"], "click_errors": errors}
    by_key = lambda rows: {(row["news_id"], row["content_type"]): row for row in rows}
    expected, observed = by_key(gold), by_key(actual)
    errors = []
    for key in sorted(expected.keys() | observed.keys()):
        if key not in expected or key not in observed:
            errors.append({"news_id": key[0], "content_type": key[1], "error": "unexpected_row" if key not in expected else "missing_row"})
            continue
        for field, value in expected[key].items():
            if observed[key].get(field) != value:
                errors.append({"news_id": key[0], "metric": field, "expected": value, "actual": observed[key].get(field)})
    click_order = lambda rows: [row["news_id"] for row in sorted(rows, key=lambda row: (-row["clicks"], row["news_id"]))]
    return {"full_numeric_scored": True, "exact_match": not errors and len(actual) == len(gold), "errors": errors,
            "expected_click_order": click_order(gold), "actual_click_order": click_order(actual),
            "click_order_changed": click_order(gold) != click_order(actual),
            "ranking_note": "Click-count ordering only; not the project hot-score ranking."}


async def probe(case, dataset, schema, candidate, source_module):
    from app.domain.errors import HotNewsDataQualityError, Text2SqlGuardError
    from app.modules.hot_news.scoring.metric_source import HotNewsMetricQuery
    HotNewsMetricSqlTemplate = source_module.HotNewsMetricSqlTemplate
    Text2SqlNewsMetricSource = source_module.Text2SqlNewsMetricSource
    warehouse = SqliteAggregateWarehouse(dataset)
    generator = ScriptedGenerator(case["sql"], case["id"])
    kwargs = dict(warehouse=warehouse, schema=schema, generator=generator, max_rows=100)
    if candidate:
        kwargs["metric_contract"] = HotNewsMetricSqlTemplate(
            table=dataset["table"], metric_expressions=dataset["approved_metric_expressions"],
            group_by_dimensions=False, bucket_seconds=dataset["approved_bucket_seconds"], version="business-hourly-v1",
        )
    started = monotonic()
    result = {"id": case["id"], "business_scenario": case["business_scenario"], "group": case["group"],
              "expected": case["expected"], "sql": case["sql"], "observed": None, "error": None}
    try:
        source = Text2SqlNewsMetricSource(**kwargs)
        batch = await source.fetch_batch(HotNewsMetricQuery(
            tenant_id="tenant-demo", window_start=datetime.fromisoformat(case["window_start"]),
            window_end=datetime.fromisoformat(case["window_end"]), ranking_limit=20,
            requested_metrics=frozenset(case["requested_metrics"]),
        ))
        snapshots = [{key: value for key, value in asdict(item).items() if key not in {"window_start", "window_end", "ctr"}} for item in batch.snapshots]
        result.update(observed="allow", snapshots=snapshots, numeric=assess_numbers(case, snapshots),
                      silently_dropped_requested_metrics=sorted(set(case["requested_metrics"]) - set(snapshots[0] if snapshots else ())))
    except (HotNewsDataQualityError, Text2SqlGuardError, ValueError) as exc:
        result.update(observed="reject", error={"type": type(exc).__name__, "message": str(exc)})
    except Exception as exc:
        result.update(observed="error", error={"type": type(exc).__name__, "message": str(exc)})
    finally:
        result.update(warehouse_calls=warehouse.calls, warehouse_rows=warehouse.rows, scripted_generator_calls=generator.calls,
                      real_inference_calls=0, elapsed_ms=round((monotonic() - started) * 1000, 3))
        warehouse.connection.close()
    result["passed"] = result["observed"] == case["expected"] and (case["expected"] != "allow" or result.get("numeric", {}).get("exact_match") is True)
    result["blocked_before_warehouse"] = result["observed"] == "reject" and result["warehouse_calls"] == 0
    return result


async def run(args):
    install_offline_fence()
    from app.modules.hot_news.scoring import text2sql_metric_source as source_module
    from app.schemas.text2sql import Text2SqlColumn, Text2SqlSchema, Text2SqlTable
    if args.source_file is not None:
        source_spec = importlib.util.spec_from_file_location("evaluation._frozen_metric_source", args.source_file)
        source_module = importlib.util.module_from_spec(source_spec)
        sys.modules[source_spec.name] = source_module
        source_spec.loader.exec_module(source_module)
    dataset_path = args.dataset.resolve()
    raw = dataset_path.read_bytes()
    dataset = json.loads(raw)
    candidate = "metric_contract" in inspect.signature(source_module.Text2SqlNewsMetricSource).parameters
    mode = "candidate_explicit_metric_contract" if candidate else "baseline_without_metric_contract"
    if args.require_mode and args.require_mode != ("candidate" if candidate else "baseline"):
        raise SystemExit("Actual source API does not match --require-mode; no experiment executed")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    source_file = Path(source_module.__file__).resolve()
    source_raw = source_file.read_bytes()
    evidence_dir = args.output_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "text2sql_metric_source.py").write_bytes(source_raw)
    started = datetime.now(timezone.utc).isoformat()
    command = [sys.executable, "-m", "evaluation.run_metric_contract_business", *sys.argv[1:]]
    spec = {
        "run_id": args.output_dir.name, "started_at": started, "mode": mode,
        "objective": "deterministic contract and synthetic-SQL business replay",
        "scope": "Text2SqlNewsMetricSource.fetch_batch -> real SqlGuard -> SQLite SQL -> real snapshot converter",
        "dataset": {"path": str(dataset_path), "sha256": sha(raw), "version": dataset["version"], "split": dataset["split"], "cases": len(dataset["cases"]), "aggregate_rows": len(dataset["rows"]), "label_review": dataset["label_review"]},
        "identity": identity(dataset_path), "as_of": dataset["as_of"], "timezone": dataset["timezone"],
        "executed_metric_source": {"path": str(source_file), "sha256": sha(source_raw), "frozen_copy": "evidence/text2sql_metric_source.py", "note": "Only this module is historical if --source-file is used; remaining dependencies match this run's identity files."},
        "environment": {"inference": "scripted SQL response, not a real model", "warehouse": "in-memory SQLite synthetic hourly aggregates", "postgres": "not_run", "enterprise_endpoints": "not_run", "embedding": "not_applicable", "workflow": "not_run", "external_network": "forbidden"},
        "candidate_blueprint": {"version": "business-hourly-v1", "metric_expressions": dataset["approved_metric_expressions"], "group_by_dimensions": False, "bucket_seconds": 3600},
        "oracle": {"numeric": "dataset gold_rows explicitly lists synthetic approved row values; integer exact equality", "full_cross_hour_uv": "not_scored: hourly UV cannot provide dedup gold", "expected_deny": "explicitly preregistered approved-query contract", "ranking": "click-count order only, not a substitute hot-score implementation", "production_acceptance": "not_approved"},
        "budgets": {"repeat_per_case": 1, "concurrency": 1, "real_model_calls": 0, "warehouse_calls_max": len(dataset["cases"]), "wall_seconds_max": 60},
        "limitations": dataset["limitations"], "command": command,
    }
    write_json(args.output_dir / "run-spec.json", spec)
    schema = Text2SqlSchema(tables=(Text2SqlTable(name=dataset["table"], columns=tuple(Text2SqlColumn(name=key, data_type="TEXT" if key in {"news_id", "content_type", "tenant_id", "event_time"} else "BIGINT") for key in dataset["schema_columns"])),), max_rows=100)
    results = []
    begin = monotonic()
    with (args.output_dir / "cases.jsonl").open("x", encoding="utf-8") as stream:
        for case in dataset["cases"]:
            if monotonic() - begin > 60:
                raise RuntimeError("60-second budget exceeded; preserve partial cases, do not omit from denominator")
            result = await probe(case, dataset, schema, candidate, source_module)
            results.append(result)
            stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
    normal = [row for row in results if row["expected"] == "allow"]
    denied = [row for row in results if row["expected"] == "reject"]
    formulas = [row for row in results if row["group"] == "formula"]
    numeric = [row for row in results if row["observed"] == "allow" and row.get("numeric", {}).get("full_numeric_scored")]
    summary = {
        "run_id": args.output_dir.name, "mode": mode, "status": "passed" if all(row["passed"] for row in results) else "failed",
        "production_acceptance": "not_approved", "model_quality": "not_run", "enterprise_contract": "not_run", "online_risk_rate": None,
        "planned": len(dataset["cases"]), "completed": len(results), "passed": sum(row["passed"] for row in results), "failed": sum(not row["passed"] for row in results),
        "execution_errors": sum(row["observed"] == "error" for row in results), "blocked": 0, "not_run": 0, "skipped": 0,
        "metrics": {
            "normal_success": {"count": sum(row["passed"] for row in normal), "denominator": len(normal)},
            "bad_formula_allowed": {"count": sum(row["observed"] == "allow" for row in formulas), "denominator": len(formulas)},
            "bad_query_allowed": {"count": sum(row["observed"] == "allow" for row in denied), "denominator": len(denied)},
            "bad_query_rejected": {"count": sum(row["observed"] == "reject" for row in denied), "denominator": len(denied)},
            "bad_query_blocked_before_warehouse": {"count": sum(row["blocked_before_warehouse"] for row in denied), "denominator": len(denied)},
            "returned_task_numeric_error": {"count": sum(not row["numeric"]["exact_match"] for row in numeric), "denominator": len(numeric), "note": "Only returned, full-gold tasks; rejection has no numeric output. Not online risk."},
            "click_order_changed": {"count": sum(row["numeric"].get("click_order_changed", False) for row in numeric), "denominator": len(numeric), "note": "Click order only, not final hot-score ranking."},
        },
        "warehouse_calls": sum(row["warehouse_calls"] for row in results), "real_inference_calls": 0,
        "elapsed_ms": round((monotonic() - begin) * 1000, 3), "limitations": dataset["limitations"],
    }
    write_json(args.output_dir / "results.json", summary)
    write_json(args.output_dir / "commands.json", {"command": command, "cwd": str(Path.cwd()), "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(), "exit_code": 0, "note": "Exit 0 means replay completed. See results.status for contract verdict."})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--require-mode", choices=("baseline", "candidate"))
    parser.add_argument("--source-file", type=Path, help="Replay an exact preserved metric-source module; all other dependencies stay at frozen current identity.")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
