"""The independent v8 deployment cannot touch or silently replace old services."""

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import start_real_news_warehouse as deploy


@pytest.fixture
def package(tmp_path):
    body = "冻结的真实新闻正文，仅用于部署测试。"
    row = {
        "tenant_id": "11111111-1111-4111-8111-111111111111", "news_id": "20260903A05YQE00",
        "title": "WorkBuddy开放平台上线，AI办公迎来硬件入口之争",
        "source_url": "https://news.qq.com/rain/a/20260903A05YQE00",
        "publish_time": "2026-09-03T12:18:15+08:00", "content_sha256": sha256(body.encode()).hexdigest(),
        "source_file": "news/data/local_recall/articles-v1/20260903A05YQE00.json",
        "source_sha256": "a" * 64, "source_package": "corpus-v6", "source_manifest_sha256": "b" * 64,
        "source": "腾讯新闻公开页面缓存", "content_type": "article", "news_content_synthetic": False,
        "content": body,
    }
    counts = {name: 2 for name in deploy.TABLES}
    counts["dim_news"] = 1
    manifest = {"dataset_sha256": "0" * 64, "tables": {name: {"rows": count} for name, count in counts.items()},
                "closed_window_start": "2026-10-07T00:00:00+08:00", "open_window_start": "2026-10-14T19:00:00+08:00"}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "dim_news.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp_path / "warehouse.sql").write_text("\\set ON_ERROR_STOP on\nBEGIN;\nCREATE SCHEMA news_sim_v8;\nCOMMIT;\n", encoding="utf-8")
    result = {"counts": counts, "window_min": manifest["closed_window_start"],
              "window_max": manifest["open_window_start"], "metric_min": manifest["closed_window_start"],
              "metric_max": manifest["open_window_start"],
              "dimensions": [{**{field: row[field] for field in deploy._DIM_FIELDS},
                              "actual_content_sha256": row["content_sha256"]}]}
    return tmp_path, manifest, result


def _validator(path):
    return {"dataset_sha256": "0" * 64, "status": "passed"}


def _runner(result, calls, *, volume_digest="0" * 64, absent=False):
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["docker", "volume", "inspect"]:
            if absent:
                return subprocess.CompletedProcess(command, 1, "", "Error response from daemon: no such volume")
            volume = {"Name": deploy.VOLUME, "Labels": {deploy.DATASET_LABEL: volume_digest}}
            return subprocess.CompletedProcess(command, 0, json.dumps([volume]), "")
        if "up" in command:
            return subprocess.CompletedProcess(command, 0, "started", "")
        if "port" in command:
            return subprocess.CompletedProcess(command, 0, "127.0.0.1:28438\n", "")
        if "exec" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(result, ensure_ascii=False) + "\n", "")
        raise AssertionError(f"unexpected command {command}")
    return run


class _FakePostgresSocket:
    def __init__(self, response=b"N"):
        self.response = response
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        pass

    def settimeout(self, timeout):
        assert timeout == 5

    def sendall(self, payload):
        assert payload == b"\x00\x00\x00\x08\x04\xd2\x16\x2f"
        self.sent = payload

    def recv(self, size):
        assert size == 1
        return self.response


def _connector(address, timeout):
    assert address == ("127.0.0.1", 28438) and timeout == 5
    return _FakePostgresSocket()


def test_compose_exposes_only_new_local_postgres_with_atomic_readonly_import():
    config = yaml.safe_load(deploy.COMPOSE_PATH.read_text(encoding="utf-8"))
    assert config["name"] == deploy.PROJECT
    assert set(config["services"]) == {"warehouse"}
    service = config["services"]["warehouse"]
    assert service["image"] == "postgres:16"
    assert service["ports"] == ["127.0.0.1:28438:5432"]
    assert service["mem_limit"] == service["memswap_limit"] == "1g"
    assert service["environment"]["POSTGRES_PASSWORD"] == "local-v8-only"
    assert "env_file" not in service
    mount = next(value for value in service["volumes"] if value["type"] == "bind")
    assert mount["read_only"] is True
    assert mount["target"].endswith("/010-warehouse.sql")
    assert mount["source"].endswith("/warehouse.sql")
    assert mount["bind"]["create_host_path"] is False
    assert "-h 127.0.0.1" in service["healthcheck"]["test"][1]
    assert config["networks"]["warehouse-bridge"]["internal"] is False
    assert config["networks"]["warehouse-bridge"]["driver"] == "bridge"
    assert config["networks"]["warehouse-bridge"]["name"] == f"{deploy.PROJECT}-bridge"
    assert config["volumes"]["warehouse-data"]["name"] == deploy.VOLUME
    assert deploy.DATASET_LABEL in config["volumes"]["warehouse-data"]["labels"]


@pytest.mark.parametrize("absent", [False, True])
def test_start_validates_first_uses_fixed_new_project_and_verifies_every_dimension(package, absent):
    path, manifest, result = package
    calls, events = [], []
    def validate(directory):
        assert directory == path.absolute()
        assert not calls
        events.append("validated")
        return _validator(directory)
    report = deploy.start_warehouse(path, validator=validate, run=_runner(result, calls, absent=absent),
                                   connector=_connector,
                                   environ={"PATH": "/bin", "HOME": "/real/user", "OPENAI_API_KEY": "secret",
                                            "DATABASE_URL": "enterprise-secret", "COMPOSE_FILE": "old-stack.yml"})
    assert events == ["validated"] and report["status"] == "ready"
    assert report["agent_integration"] == "not_connected"
    assert report["database_verification"]["real_news_dimensions_verified"] == 1
    assert len(calls) == 4
    up, kwargs = calls[1]
    assert up == deploy.compose_command("up", "--detach", "--wait", "--wait-timeout", "300", "warehouse")
    assert "--env-file" in up and up[up.index("--env-file") + 1] == os.devnull
    assert kwargs["env"]["REAL_NEWS_WAREHOUSE_DATASET"] == str(path.absolute())
    assert kwargs["env"]["REAL_NEWS_WAREHOUSE_DATASET_SHA256"] == manifest["dataset_sha256"]
    assert not {"OPENAI_API_KEY", "DATABASE_URL", "COMPOSE_FILE"}.intersection(kwargs["env"])
    for command, _ in calls:
        assert not {"down", "rm", "api", "hot-news-worker", "writing-worker", "volume-rm"}.intersection(command)
    assert calls[2][0] == deploy.compose_command("port", "warehouse", "5432")
    assert report["host_connection"]["protocol"] == "PostgreSQL SSLRequest"
    query = calls[3][0][-1]
    assert query.startswith("BEGIN READ ONLY;")
    assert all(f"FROM news_sim_v8.{name}" in query for name in deploy.TABLES)
    assert "sha256(convert_to(content,'UTF8'))" in query


def test_skip_start_checks_complete_package_without_any_docker_action(package):
    path, _, _ = package
    def no_run(*args, **kwargs):
        raise AssertionError("skip-start must not invoke Docker")
    report = deploy.start_warehouse(path, skip_start=True, validator=_validator, run=no_run)
    assert report["status"] == "validated"
    assert report["local_dsn"] == deploy.LOCAL_DSN


def test_invalid_package_prevents_any_docker_mutation(package):
    path, _, _ = package
    def invalid(_):
        raise ValueError("bad package digest")
    calls = []
    with pytest.raises(ValueError, match="bad package digest"):
        deploy.start_warehouse(path, validator=invalid, run=lambda *a, **kw: calls.append(a))
    assert not calls


@pytest.mark.parametrize("digest", [None, "1" * 64])
def test_different_or_unlabeled_existing_volume_is_preserved_without_up(package, digest):
    path, _, result = package
    calls = []
    with pytest.raises(deploy.WarehouseDeploymentError, match="different or unverified dataset"):
        deploy.start_warehouse(path, validator=_validator, run=_runner(result, calls, volume_digest=digest))
    assert len(calls) == 1
    assert calls[0][0] == ["docker", "volume", "inspect", deploy.VOLUME]


@pytest.mark.parametrize("field", ["title", "source_url", "source_sha256", "source_manifest_sha256", "actual_content_sha256"])
def test_existing_database_content_or_provenance_drift_is_rejected(package, field):
    path, manifest, original = package
    result = deepcopy(original)
    result["dimensions"][0][field] = "different"
    with pytest.raises(deploy.WarehouseDeploymentError, match="differs"):
        deploy.verify_database_result(path, manifest, result)


@pytest.mark.parametrize("field", ["window_min", "window_max", "metric_min", "metric_max"])
def test_existing_database_statistics_window_drift_is_rejected(package, field):
    path, manifest, original = package
    result = deepcopy(original)
    result[field] = "2000-01-01T00:00:00+08:00"
    with pytest.raises(deploy.WarehouseDeploymentError, match="statistics window differs"):
        deploy.verify_database_result(path, manifest, result)


def test_existing_database_count_drift_is_rejected(package):
    path, manifest, original = package
    result = deepcopy(original)
    result["counts"]["news_metric_daily"] += 1
    with pytest.raises(deploy.WarehouseDeploymentError, match="table counts differ"):
        deploy.verify_database_result(path, manifest, result)


def test_nonatomic_sql_prevents_any_docker_action(package):
    path, _, _ = package
    (path / "warehouse.sql").write_text("CREATE SCHEMA news_sim_v8;\n", encoding="utf-8")
    with pytest.raises(deploy.WarehouseDeploymentError, match="atomic BEGIN/COMMIT"):
        deploy.start_warehouse(path, validator=_validator, run=lambda *a, **kw: pytest.fail("Docker was invoked"))


def test_internal_health_without_actual_published_port_cannot_report_ready(package):
    path, _, result = package
    calls = []
    normal = _runner(result, calls)
    def run(command, **kwargs):
        if "port" in command:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")
        return normal(command, **kwargs)
    with pytest.raises(deploy.WarehouseDeploymentError, match="did not publish"):
        deploy.start_warehouse(path, validator=_validator, run=run,
                               connector=lambda *a, **kw: pytest.fail("connected before mapping verification"))
    assert len(calls) == 3 and all("exec" not in command for command, _ in calls)


@pytest.mark.parametrize("response", [b"", b"H", b"E"])
def test_non_postgres_host_protocol_response_is_rejected(response):
    with pytest.raises(deploy.WarehouseDeploymentError, match="protocol negotiation"):
        deploy._verify_host_postgres(connector=lambda *a, **kw: _FakePostgresSocket(response))


def test_closed_host_port_is_rejected_even_when_container_is_healthy(package):
    path, _, result = package
    calls = []
    def refused(*args, **kwargs):
        raise ConnectionRefusedError("closed port")
    with pytest.raises(deploy.WarehouseDeploymentError, match="internal container health is insufficient"):
        deploy.start_warehouse(path, validator=_validator, run=_runner(result, calls), connector=refused)
    assert len(calls) == 3 and all("exec" not in command for command, _ in calls)


@pytest.mark.parametrize("wait", [0, -1, 1801, True])
def test_invalid_wait_does_not_start_or_read_data(wait):
    with pytest.raises(ValueError, match="wait-seconds"):
        deploy.start_warehouse("missing-directory", wait_seconds=wait, validator=lambda _: pytest.fail("validated data"))
