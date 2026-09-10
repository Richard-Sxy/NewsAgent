"""Opt-in black-box acceptance test for the hermetic Compose stack."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse
from uuid import UUID, uuid4

import boto3
import psycopg
from psycopg import sql
import pytest


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("RUN_DATA_LOOP_E2E") != "1",
        reason="set RUN_DATA_LOOP_E2E=1 against the dedicated E2E stack",
    ),
]


def run_driver(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "examples.data_loop_e2e",
        *args,
    ]
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def assert_succeeded(completed: subprocess.CompletedProcess[str]) -> None:
    assert completed.returncode == 0, completed.stdout + completed.stderr


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def database_dsn() -> str:
    value = os.environ.get("DATABASE_URL", "")
    assert value, "DATABASE_URL is required for the recovery fault drill"
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def assert_artifacts_are_hash_verified(tenant_id: str) -> None:
    with psycopg.connect(database_dsn()) as connection:
        artifacts = connection.execute(
            "SELECT artifact_uri, content_sha256 FROM evaluation_datasets "
            "WHERE tenant_id = %s UNION ALL "
            "SELECT artifact_uri, artifact_sha256 FROM candidate_evaluation_runs "
            "WHERE tenant_id = %s",
            (tenant_id, tenant_id),
        ).fetchall()
    assert len(artifacts) == 4
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["ARTIFACT_ENDPOINT"],
        region_name=os.environ.get("ARTIFACT_REGION", "us-east-1"),
    )
    for artifact_uri, expected_sha256 in artifacts:
        parsed = urlparse(artifact_uri)
        assert parsed.scheme == "s3" and parsed.netloc and parsed.path
        response = client.get_object(
            Bucket=parsed.netloc,
            Key=parsed.path.lstrip("/"),
        )
        content = response["Body"].read()
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        assert response["Metadata"]["content-sha256"] == expected_sha256
        assert response["Metadata"]["artifact-kind"] in {
            "evaluation-dataset",
            "data-loop",
        }


def test_data_loop_approve_next_run_and_rollback(tmp_path: Path) -> None:
    state_path = tmp_path / "data-loop-approve.json"
    completed = run_driver(
        "auto",
        "--state",
        str(state_path),
        "--action",
        "approve",
        "--rollback",
    )
    assert_succeeded(completed)

    state = load_state(state_path)
    assert state["terminal_phase"] == "activated"
    assert state["post_activation_run_id"]
    assert state["rolled_back"] is True
    assert all(state["checks"].values())
    assert_artifacts_are_hash_verified(state["tenant_id"])

    recovery = run_driver("recover", "--state", str(state_path))
    assert recovery.returncode != 0
    assert "cannot override" in recovery.stderr


def test_data_loop_reject_keeps_base_active(tmp_path: Path) -> None:
    state_path = tmp_path / "data-loop-reject.json"
    completed = run_driver(
        "auto",
        "--state",
        str(state_path),
        "--action",
        "reject",
    )
    assert_succeeded(completed)

    state = load_state(state_path)
    assert state["decision"] == "reject"
    assert state["terminal_phase"] == "rejected"
    assert state["activated_bundle_id"] is None
    assert state["checks"]["rejection_keeps_base_active"] is True
    assert all(state["checks"].values())


def test_data_loop_recovers_only_an_authorized_failed_activation(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "data-loop-recovery.json"
    prepared = run_driver("prepare", "--state", str(state_path))
    assert_succeeded(prepared)
    state = load_state(state_path)
    tenant_id = str(UUID(state["tenant_id"]))
    candidate_id = str(UUID(state["candidate_id"]))
    suffix = uuid4().hex
    function_name = f"e2e_block_activate_{suffix}"
    trigger_name = f"e2e_block_activation_{suffix}"

    create_function = sql.SQL(
        """
        CREATE FUNCTION {function_name}() RETURNS trigger LANGUAGE plpgsql AS $fn$
        BEGIN
          IF NEW.action = 'activate'
             AND NEW.tenant_id = {tenant_id}
             AND NEW.candidate_id = {candidate_id}::uuid THEN
            RAISE EXCEPTION 'E2E forced activation failure';
          END IF;
          RETURN NEW;
        END
        $fn$
        """
    ).format(
        function_name=sql.Identifier(function_name),
        tenant_id=sql.Literal(tenant_id),
        candidate_id=sql.Literal(candidate_id),
    )
    create_trigger = sql.SQL(
        "CREATE TRIGGER {trigger_name} BEFORE INSERT ON promotion_decisions "
        "FOR EACH ROW EXECUTE FUNCTION {function_name}()"
    ).format(
        trigger_name=sql.Identifier(trigger_name),
        function_name=sql.Identifier(function_name),
    )
    drop_trigger = sql.SQL(
        "DROP TRIGGER IF EXISTS {trigger_name} ON promotion_decisions"
    ).format(trigger_name=sql.Identifier(trigger_name))
    drop_function = sql.SQL("DROP FUNCTION IF EXISTS {function_name}()").format(
        function_name=sql.Identifier(function_name)
    )

    with psycopg.connect(database_dsn(), autocommit=True) as connection:
        connection.execute(create_function)
        connection.execute(create_trigger)
    try:
        blocked = run_driver(
            "decide",
            "--state",
            str(state_path),
            "--action",
            "approve",
            "--skip-next-online-run",
            "--timeout-seconds",
            "20",
            timeout=90,
        )
        assert blocked.returncode != 0
        assert "phase 'activated'" in blocked.stderr

        blocked_state = load_state(state_path)
        assert blocked_state["decision"] == "approve"
        assert blocked_state["terminal_phase"] is None
        with psycopg.connect(database_dsn()) as connection:
            rows = connection.execute(
                "SELECT action, count(*) FROM promotion_decisions "
                "WHERE tenant_id = %s GROUP BY action",
                (tenant_id,),
            ).fetchall()
        assert dict(rows) == {"approve": 1}
    finally:
        with psycopg.connect(database_dsn(), autocommit=True) as connection:
            connection.execute(drop_trigger)
            connection.execute(drop_function)

    recovered = run_driver("recover", "--state", str(state_path))
    assert_succeeded(recovered)
    recovered_state = load_state(state_path)
    assert recovered_state["terminal_phase"] == "activation_recovered"
    assert recovered_state["post_activation_run_id"]
    assert all(recovered_state["checks"].values())

    with psycopg.connect(database_dsn()) as connection:
        actions = connection.execute(
            "SELECT action, count(*) FROM promotion_decisions "
            "WHERE tenant_id = %s GROUP BY action",
            (tenant_id,),
        ).fetchall()
        active_count = connection.execute(
            "SELECT count(*) FROM production_bundles "
            "WHERE tenant_id = %s AND status = 'active'",
            (tenant_id,),
        ).fetchone()
    assert dict(actions) == {"activate": 1, "approve": 1}
    assert active_count == (1,)
