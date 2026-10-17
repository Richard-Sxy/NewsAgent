#!/usr/bin/env python3
"""Start only the isolated v8 PostgreSQL fixture; no existing Agent is changed.

The fixed local password is deliberately public demo configuration. The script
never loads .env, deletes volumes, or attaches this fixture to an Agent. A
dataset digest labels the new volume so another package cannot silently reuse
it. Existing data is also inspected with read-only SQL after startup.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
from typing import Any

TOOLS_DIR = str(Path(__file__).resolve().parent)
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from validate_real_news_warehouse import TABLES, validate_package

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = Path(__file__).resolve().parents[1] / "deploy/docker-compose.real-news-warehouse-v8.yml"
DEFAULT_DATASET = REPOSITORY_ROOT / "output/warehouse/real-news-v8-20261014"
PROJECT = "newsagent-real-news-warehouse-v8"
VOLUME = "newsagent-real-news-warehouse-v8-pgdata"
DATASET_LABEL = "org.newsagent.dataset_sha256"
LOCAL_DSN = "postgresql://news_demo:local-v8-only@127.0.0.1:28438/news_demo"
MAX_DATABASE_RESPONSE = 4 * 1024 * 1024
_DIM_FIELDS = (
    "tenant_id", "news_id", "title", "source_url", "publish_time", "content_sha256",
    "source_file", "source_sha256", "source_package", "source_manifest_sha256",
    "source", "content_type", "news_content_synthetic",
)


class WarehouseDeploymentError(RuntimeError):
    pass


def compose_command(*arguments: str) -> list[str]:
    return ["docker", "compose", "--env-file", os.devnull, "--project-name", PROJECT,
            "--file", str(COMPOSE_PATH), *arguments]


def _environment(dataset: Path, digest: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    supplied = os.environ if environ is None else environ
    # Preserve Docker transport and user configuration, without passing model,
    # database, enterprise, or unrelated Compose environment overrides.
    permitted = {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                 "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "SYSTEMROOT", "TMPDIR"}
    result = {key: value for key, value in supplied.items() if key in permitted}
    result["REAL_NEWS_WAREHOUSE_DATASET"] = str(dataset)
    result["REAL_NEWS_WAREHOUSE_DATASET_SHA256"] = digest
    return result


def _run(command: list[str], *, env: dict[str, str], timeout: int, run=None,
         check: bool = True) -> subprocess.CompletedProcess:
    execute = subprocess.run if run is None else run
    try:
        result = execute(command, cwd=str(REPOSITORY_ROOT), env=env, text=True,
                         capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WarehouseDeploymentError(f"Local Docker command failed: {type(exc).__name__}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "no output")[-2000:]
        raise WarehouseDeploymentError(f"Local warehouse command failed: {detail}")
    return result


def _verify_transaction(path: Path) -> None:
    with path.open("rb") as stream:
        prefix = stream.read(128)
        stream.seek(max(0, path.stat().st_size - 256))
        suffix = stream.read(256)
    if not prefix.startswith(b"\\set ON_ERROR_STOP on\nBEGIN;\n") or not suffix.rstrip().endswith(b"COMMIT;"):
        raise WarehouseDeploymentError("Frozen warehouse SQL requires ON_ERROR_STOP and one atomic BEGIN/COMMIT import")


def _check_volume_identity(digest: str, *, env: dict[str, str], run=None) -> None:
    result = _run(["docker", "volume", "inspect", VOLUME], env=env, timeout=30, run=run, check=False)
    if result.returncode:
        if "no such volume" in (result.stderr or "").lower():
            return
        raise WarehouseDeploymentError("Cannot inspect the independent warehouse volume")
    try:
        volumes = json.loads(result.stdout)
        if not isinstance(volumes, list) or len(volumes) != 1 or volumes[0].get("Name") != VOLUME:
            raise ValueError("unexpected volume identity")
        bound = (volumes[0].get("Labels") or {}).get(DATASET_LABEL)
    except (ValueError, TypeError, AttributeError) as exc:
        raise WarehouseDeploymentError("Invalid independent warehouse volume metadata") from exc
    if bound != digest:
        raise WarehouseDeploymentError(
            "Existing v8 volume belongs to a different or unverified dataset; stopped without deleting or replacing it")


def _verify_host_postgres(*, connector=None) -> dict[str, Any]:
    """Check the host path and PostgreSQL wire protocol without SQL or a password.

    PostgreSQL SSLRequest is an eight-byte protocol negotiation request. A
    PostgreSQL server responds with exactly N (SSL disabled) or S (SSL enabled).
    This detects a closed host port or an unrelated service, while issuing no
    application query and never loading credentials.
    """
    connect = socket.create_connection if connector is None else connector
    try:
        with connect(("127.0.0.1", 28438), timeout=5) as connection:
            connection.settimeout(5)
            connection.sendall(struct.pack("!II", 8, 80877103))
            response = connection.recv(1)
    except OSError as exc:
        raise WarehouseDeploymentError(
            "Host PostgreSQL endpoint 127.0.0.1:28438 is unreachable; internal container health is insufficient") from exc
    if response not in {b"N", b"S"}:
        raise WarehouseDeploymentError("Host port 28438 did not answer the PostgreSQL protocol negotiation")
    return {"status": "passed", "host": "127.0.0.1", "port": 28438,
            "protocol": "PostgreSQL SSLRequest", "server_ssl": response == b"S"}


def database_verification_sql() -> str:
    counts = ",".join(f"'{name}',(SELECT count(*) FROM news_sim_v8.{name})" for name in TABLES)
    fields = ",".join(f"'{field}',{field}" for field in _DIM_FIELDS)
    return f"""BEGIN READ ONLY;
SET LOCAL TIME ZONE 'Asia/Shanghai';
SET LOCAL statement_timeout = '120s';
SELECT json_build_object(
 'counts',json_build_object({counts}),
 'window_min',(SELECT min(event_time) FROM news_sim_v8.window_snapshot),
 'window_max',(SELECT max(event_time) FROM news_sim_v8.window_snapshot),
 'metric_min',(SELECT min(event_time) FROM news_sim_v8.news_metric_hourly),
 'metric_max',(SELECT max(event_time) FROM news_sim_v8.news_metric_hourly),
 'dimensions',(SELECT json_agg(json_build_object({fields},
   'actual_content_sha256',encode(sha256(convert_to(content,'UTF8')),'hex')) ORDER BY tenant_id,news_id)
   FROM news_sim_v8.dim_news));
COMMIT;
"""


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise WarehouseDeploymentError("Database metric timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WarehouseDeploymentError("Database metric timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise WarehouseDeploymentError("Database timestamp must include its timezone")
    return parsed


def verify_database_result(dataset: Path, manifest: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    expected_counts = {name: manifest["tables"][name]["rows"] for name in TABLES}
    if result.get("counts") != expected_counts:
        raise WarehouseDeploymentError("Existing database table counts differ from the verified dataset; volume preserved")
    for actual, expected in (("window_min", "closed_window_start"), ("window_max", "open_window_start"),
                             ("metric_min", "closed_window_start"), ("metric_max", "open_window_start")):
        if _timestamp(result.get(actual)) != _timestamp(manifest.get(expected)):
            raise WarehouseDeploymentError("Existing database statistics window differs from the verified dataset; volume preserved")
    dimensions = result.get("dimensions")
    if not isinstance(dimensions, list) or len(dimensions) != expected_counts["dim_news"]:
        raise WarehouseDeploymentError("Existing database real-news dimensions are incomplete")
    observed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in dimensions:
        if not isinstance(row, dict) or set(row) != set(_DIM_FIELDS) | {"actual_content_sha256"}:
            raise WarehouseDeploymentError("Unexpected database news dimension projection")
        key = (row["tenant_id"], row["news_id"])
        if key in observed:
            raise WarehouseDeploymentError("Duplicate database news dimension identity")
        observed[key] = row
    with (dataset / "dim_news.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            expected = json.loads(line)
            key = (expected["tenant_id"], expected["news_id"])
            actual = observed.pop(key, None)
            if actual is None:
                raise WarehouseDeploymentError("Database does not contain the verified real-news identity")
            for field in _DIM_FIELDS:
                same = (_timestamp(actual[field]) == _timestamp(expected[field]) if field == "publish_time"
                        else actual[field] == expected[field])
                if not same:
                    raise WarehouseDeploymentError(f"Database real-news {field} differs from frozen content; volume preserved")
            if actual["actual_content_sha256"] != sha256(expected["content"].encode()).hexdigest():
                raise WarehouseDeploymentError("Database real-news body differs from frozen content; volume preserved")
    if observed:
        raise WarehouseDeploymentError("Database contains unexpected real-news dimensions")
    return {"status": "passed", "table_counts": expected_counts,
            "real_news_dimensions_verified": expected_counts["dim_news"],
            "statistics_window_start": manifest["closed_window_start"],
            "open_window_start": manifest["open_window_start"]}


def start_warehouse(dataset: Path | str = DEFAULT_DATASET, *, wait_seconds: int = 300,
                    skip_start: bool = False, environ: dict[str, str] | None = None,
                    run=None, validator=None, connector=None) -> dict[str, Any]:
    if type(wait_seconds) is not int or not 1 <= wait_seconds <= 1800:
        raise ValueError("wait-seconds must be an integer from 1 through 1800")
    selected = Path(dataset).absolute()
    # Validate all artifacts before any Docker or subprocess action.
    validation = (validate_package if validator is None else validator)(selected)
    manifest = json.loads((selected / "manifest.json").read_text(encoding="utf-8"))
    digest = validation["dataset_sha256"]
    if manifest.get("dataset_sha256") != digest:
        raise WarehouseDeploymentError("Validated dataset identity changed before deployment")
    _verify_transaction(selected / "warehouse.sql")
    env = _environment(selected, digest, environ)
    report: dict[str, Any] = {"status": "validated" if skip_start else "starting", "dataset": str(selected),
        "dataset_sha256": digest, "project": PROJECT, "volume": VOLUME,
        "local_dsn": LOCAL_DSN, "credential_kind": "public_local_demo_only",
        "news_content_synthetic": False, "metric_data_synthetic": True,
        "agent_integration": "not_connected"}
    if skip_start:
        return report
    _check_volume_identity(digest, env=env, run=run)
    _run(compose_command("up", "--detach", "--wait", "--wait-timeout", str(wait_seconds), "warehouse"),
         env=env, timeout=wait_seconds + 60, run=run)
    publication = _run(compose_command("port", "warehouse", "5432"), env=env, timeout=30, run=run)
    if publication.stdout.strip() != "127.0.0.1:28438":
        raise WarehouseDeploymentError("Docker did not publish the required loopback PostgreSQL port; volume preserved")
    report["host_connection"] = _verify_host_postgres(connector=connector)
    sql_result = _run(compose_command("exec", "-T", "-e", "PGPASSWORD=local-v8-only", "warehouse", "psql",
                                      "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1",
                                      "-U", "news_demo", "-d", "news_demo", "-c", database_verification_sql()),
                      env=env, timeout=150, run=run)
    if len(sql_result.stdout.encode()) > MAX_DATABASE_RESPONSE:
        raise WarehouseDeploymentError("Database verification response exceeded its size budget")
    try:
        database_result = json.loads(sql_result.stdout)
    except (ValueError, TypeError) as exc:
        raise WarehouseDeploymentError("Database verification did not return one JSON snapshot") from exc
    if not isinstance(database_result, dict):
        raise WarehouseDeploymentError("Invalid database verification result")
    report["database_verification"] = verify_database_result(selected, manifest, database_result)
    report["status"] = "ready"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--wait-seconds", type=int, default=300)
    parser.add_argument("--skip-start", action="store_true", help="Verify the complete package and print its future local connection only")
    args = parser.parse_args(argv)
    try:
        report = start_warehouse(args.dataset, wait_seconds=args.wait_seconds, skip_start=args.skip_start)
    except (ValueError, OSError, KeyError, WarehouseDeploymentError) as exc:
        parser.exit(2, f"Independent warehouse deployment failed: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
