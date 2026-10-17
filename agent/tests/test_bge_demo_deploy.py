"""Offline deployment contracts: no Docker, network, or local secret files."""

import importlib.util
import ast
import inspect
import io
import json
import re
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
import yaml


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = AGENT_ROOT / "tools/deploy_bge_demo.py"
PROJECT = "news-agent-bge-demo"
BASE_OVERLAYS = (
    "docker-compose.native-e2e.yml",
    "docker-compose.bge-snapshot.yml",
    "docker-compose.bge-demo.yml",
)
MODEL_OVERLAY = "docker-compose.timeline-query-model.yml"
PUBLIC_TEST_KEY = "fixture-only-model-key-never-print"


@pytest.fixture(scope="module")
def deployment_module():
    name = "newsagent_bge_demo_deployment_test_module"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through the registered module namespace.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


@pytest.fixture
def agent_root(tmp_path):
    root = tmp_path / "agent"
    deploy = root / "deploy"
    deploy.mkdir(parents=True)
    for name in (*BASE_OVERLAYS, MODEL_OVERLAY):
        (deploy / name).write_text("services: {}\n", encoding="utf-8")
    for name in (
        "model-runtime.local.yml", "model-runtime.compatible.yml",
        "knowledge-search.bge-snapshot.yml", "text2sql-scenes.local.yml", "text2sql-scenes.timeline-v4.yml",
    ):
        (deploy / name).write_text("fixture: true\n", encoding="utf-8")
    (root / "Dockerfile.e2e").write_text("FROM scratch\n", encoding="utf-8")
    (root / "Dockerfile.e2e.dockerignore").write_text(".env\n", encoding="utf-8")
    (root / "frontend/nginx").mkdir(parents=True)
    (root / "frontend/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "frontend/Dockerfile.bge-demo").write_text("FROM scratch\n", encoding="utf-8")
    (root / "frontend/Dockerfile.bge-demo.dockerignore").write_text(".env\n", encoding="utf-8")
    (root / "frontend/package-lock.json").write_text("{}\n", encoding="utf-8")
    (deploy / "bge-demo.nginx.conf").write_text("fixture\n", encoding="utf-8")
    (deploy / "bge-demo.frontend.json").write_text("{}\n", encoding="utf-8")
    (root.parent / "rag-recall-demo").mkdir()
    for name in ("SHA256SUMS", "data/experiments/offline_recall_fg/chunks.jsonl",
                 "data/experiments/offline_recall_fg/embeddings.npy",
                 "data/experiments/offline_recall_fg/index.bin", "data/models/bge-large-zh-v1.5/modules.json"):
        path = root.parent / "rag-recall-demo" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    return root


def deployment(module, root, *, model="compatible", **options):
    environ = {"NEWSAGENT_MODEL_API_KEY": PUBLIC_TEST_KEY} if model == "compatible" else {}
    return module.BgeDemoDeployment(
        module.DeploymentOptions(agent_root=root, model=model, **options), environ=environ,
    )


def compose_overlays(command):
    return [Path(command[index + 1]).name for index, value in enumerate(command) if value == "-f"]


class SocketProbe:
    def __init__(self, bindings, conflicts):
        self.bindings, self.conflicts = bindings, conflicts

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def bind(self, address):
        self.bindings.append(address)
        if address[1] in self.conflicts:
            raise OSError("fixture occupied port")


class DockerHarness:
    """No command crosses the subprocess boundary during deployment tests."""

    def __init__(self, module, instance, monkeypatch):
        self.module, self.instance = module, instance
        self.calls, self.events, self.bindings = [], [], []
        self.conflicts, self.rows = set(), []
        self.version, self.memory = "v2.39.4", 8 * module.GIB
        self.endpoint, self.fail_when = "unix:///fixture/docker.sock", None
        monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=50 * module.GIB))
        monkeypatch.setattr(module.shutil, "which", lambda *args, **kwargs: "/fixture/docker")
        monkeypatch.setattr(module.socket, "socket", lambda *args: SocketProbe(self.bindings, self.conflicts))
        monkeypatch.setattr(instance, "_run", self.run)
        monkeypatch.setattr(instance, "_wait_for_infrastructure", lambda *args, **kwargs: self.events.append("infrastructure-ready"))
        monkeypatch.setattr(instance, "_wait_until_ready", lambda *args, **kwargs: self.events.append("application-ready"))

    def run(self, command, *, runtime=False, timeout=30, capture=True):
        call = {"command": list(command), "runtime": runtime, "timeout": timeout, "capture": capture}
        self.calls.append(call)
        self.events.append(list(command))
        if self.fail_when and self.fail_when(command):
            raise self.module.DeploymentError("fixture controlled failure")
        if command[:3] == ["docker", "context", "inspect"]:
            return json.dumps(self.endpoint)
        if command[:3] == ["docker", "context", "show"]:
            return "desktop-linux\n"
        if command[:2] == ["docker", "version"]:
            return "28.0.1\n"
        if command[:3] == ["docker", "compose", "version"]:
            return self.version
        if command[:2] == ["docker", "info"]:
            return str(self.memory)
        if "ps" in command:
            return json.dumps(self.rows)
        return ""


def mutating_calls(harness):
    return [call for call in harness.calls if any(value in call["command"] for value in ("build", "up", "run", "stop", "down"))]


def test_deployment_orders_isolated_build_dependencies_migration_and_application_start(deployment_module, agent_root, monkeypatch, capsys):
    instance = deployment(deployment_module, agent_root)
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    instance.deploy()
    calls = mutating_calls(harness)
    assert len(calls) == 4
    build, infrastructure, migration, applications = calls
    assert build["command"][-5:] == ["--progress", "plain", "build", "migrate", "web"]
    assert compose_overlays(build["command"]) == list(BASE_OVERLAYS) and build["runtime"] is False
    assert infrastructure["command"][-8:] == ["up", "-d", *deployment_module.INFRA_SERVICES]
    assert migration["command"][-4:] == ["run", "--rm", "--no-deps", "migrate"]
    assert applications["command"][-12:] == ["up", "-d", "--no-deps", "--no-build", "--pull", "never", *deployment_module.APP_SERVICES]
    assert applications["runtime"] is True
    assert compose_overlays(applications["command"]) == [*BASE_OVERLAYS, MODEL_OVERLAY]
    assert harness.events.index("infrastructure-ready") < harness.events.index(migration["command"])
    assert harness.events.index(migration["command"]) < harness.events.index(applications["command"])
    assert harness.events[-1] == "application-ready"
    output = capsys.readouterr().out
    assert "http://127.0.0.1:5178/chat" in output and "http://127.0.0.1:28060/ready" in output
    assert PUBLIC_TEST_KEY not in output and all(PUBLIC_TEST_KEY not in " ".join(call["command"]) for call in harness.calls)
    assert all(call["timeout"] <= 10 for call in harness.calls if call["command"][:2] in (
        ["docker", "version"], ["docker", "info"],
    ) or call["command"][:3] in (["docker", "context", "inspect"], ["docker", "compose", "version"]))


@pytest.mark.parametrize("failure", ["disk", "docker-timeout", "compose-version", "memory", "remote-context", "configuration", "missing-key", "occupied-port"])
def test_preflight_failure_never_builds_or_starts_services(deployment_module, agent_root, monkeypatch, failure):
    instance = deployment(deployment_module, agent_root)
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    if failure == "disk":
        monkeypatch.setattr(deployment_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=deployment_module.GIB))
    elif failure == "docker-timeout":
        harness.fail_when = lambda command: command[:2] == ["docker", "version"]
    elif failure == "compose-version":
        harness.version = "2.24.3"
    elif failure == "memory":
        harness.memory = 5 * deployment_module.GIB
    elif failure == "remote-context":
        harness.endpoint = "tcp://remote.example:2376"
    elif failure == "configuration":
        harness.fail_when = lambda command: "config" in command
    elif failure == "missing-key":
        instance.environ.pop(deployment_module.MODEL_KEY)
        monkeypatch.setattr(deployment_module.sys.stdin, "isatty", lambda: False)
    elif failure == "occupied-port":
        harness.conflicts.add(instance.options.api_port)
    with pytest.raises(deployment_module.DeploymentError):
        instance.deploy()
    assert mutating_calls(harness) == []


@pytest.mark.parametrize("stage", ["build", "migration"])
def test_build_or_migration_failure_stops_downstream_work_and_preserves_project_volumes(deployment_module, agent_root, monkeypatch, stage):
    instance = deployment(deployment_module, agent_root)
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    harness.fail_when = (lambda command: "build" in command) if stage == "build" else (
        lambda command: "run" in command and "migrate" in command
    )
    with pytest.raises(deployment_module.DeploymentError):
        instance.deploy()
    commands = [call["command"] for call in harness.calls]
    if stage == "build":
        assert all("up" not in command and "run" not in command for command in commands)
    else:
        assert len([command for command in commands if "up" in command]) == 1
        assert all("--no-build" not in command for command in commands)
    assert "application-ready" not in harness.events
    assert all(not {"down", "stop", "prune", "--volumes", "volume"}.intersection(command) for command in commands)


def test_same_project_owned_ports_can_be_reused_but_foreign_project_cannot(deployment_module, agent_root, monkeypatch):
    instance = deployment(deployment_module, agent_root, model="local")
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    harness.conflicts.update((instance.options.api_port, instance.options.web_port))
    harness.rows = [{"Service": service, "Project": PROJECT, "State": "running",
                     "Publishers": [{"URL": "127.0.0.1", "PublishedPort": port}]}
                    for service, port in (("api", instance.options.api_port), ("web", instance.options.web_port))]
    instance.preflight()
    assert harness.bindings == []
    harness.rows[0]["Project"] = "unrelated-demonstration"
    with pytest.raises(deployment_module.DeploymentError, match="端口"):
        instance.preflight()
    assert mutating_calls(harness) == []


def test_preflight_checks_compatible_file_existence_without_reading_it_or_env_files(deployment_module, agent_root, monkeypatch):
    instance = deployment(deployment_module, agent_root)
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    original_read = Path.read_text

    def checked_read(path, *args, **kwargs):
        assert path.name != "model-runtime.compatible.yml" and not path.name.startswith(".env")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", checked_read)
    monkeypatch.setattr(deployment_module.getpass, "getpass", lambda *args: pytest.fail("provided model key must not be requested again"))
    instance.check()
    assert mutating_calls(harness) == []
    (agent_root / "deploy/model-runtime.compatible.yml").unlink()
    with pytest.raises(deployment_module.DeploymentError, match="model-runtime.compatible.yml"):
        instance.check()


def test_check_skip_build_only_inspects_images_and_never_starts_a_probe_container(deployment_module, agent_root, monkeypatch):
    instance = deployment(deployment_module, agent_root, model="local", skip_build=True)
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    instance.check()
    inspected = [call["command"][3] for call in harness.calls if call["command"][:3] == ["docker", "image", "inspect"]]
    assert inspected == [deployment_module.APP_IMAGE, deployment_module.WEB_IMAGE]
    assert mutating_calls(harness) == []


def test_skip_build_deployment_verifies_installed_dependencies_before_starting_services(deployment_module, agent_root, monkeypatch):
    instance = deployment(deployment_module, agent_root, model="local", skip_build=True)
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    instance.deploy()
    probes = [call["command"] for call in harness.calls if call["command"][:2] == ["docker", "run"]]
    assert len(probes) == 1 and "--network" in probes[0] and probes[0][probes[0].index("--network") + 1] == "none"
    assert "--read-only" in probes[0] and "--rm" in probes[0]
    assert "numpy" in probes[0][-1] and "sentence-transformers" in probes[0][-1] and "hnswlib" in probes[0][-1]
    assert all("build" not in call["command"] for call in harness.calls)
    first_up = next(call["command"] for call in harness.calls if "up" in call["command"])
    assert harness.events.index(probes[0]) < harness.events.index(first_up)
    assert "application-ready" in harness.events


@pytest.mark.parametrize("missing", ["image", "dependency"])
def test_skip_build_does_not_bypass_missing_images_dependencies_or_disk_budget(deployment_module, agent_root, monkeypatch, missing):
    instance = deployment(deployment_module, agent_root, model="local", skip_build=True)
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    harness.fail_when = (lambda command: command[:3] == ["docker", "image", "inspect"]) if missing == "image" else (
        lambda command: command[:2] == ["docker", "run"]
    )
    with pytest.raises(deployment_module.DeploymentError):
        instance.deploy()
    assert all("up" not in call["command"] for call in harness.calls)
    harness.fail_when = None
    harness.calls.clear()
    monkeypatch.setattr(deployment_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=deployment_module.GIB))
    with pytest.raises(deployment_module.DeploymentError, match="磁盘"):
        instance.deploy()
    assert harness.calls == []


def test_status_prints_only_public_fields_and_stop_retains_volumes(deployment_module, agent_root, monkeypatch, capsys):
    instance = deployment(deployment_module, agent_root)
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    harness.rows = [{"Service": "api", "State": "running", "Health": "healthy", "ExitCode": 0,
                     "Config": {"Env": [PUBLIC_TEST_KEY]}, "Labels": {"secret": PUBLIC_TEST_KEY}}]
    instance.status()
    output = capsys.readouterr().out
    assert "api" in output and "running" in output and PUBLIC_TEST_KEY not in output and "Config" not in output and "Labels" not in output
    assert len([call for call in harness.calls if "ps" in call["command"]]) == 1
    assert all(call["command"][:3] in (["docker", "context", "show"], ["docker", "context", "inspect"])
               or "ps" in call["command"] for call in harness.calls)
    instance.stop()
    assert harness.calls[-1]["command"][-1] == "stop"
    assert all("--volumes" not in call["command"] and "down" not in call["command"] for call in harness.calls)


def test_real_command_runner_strips_build_key_redacts_failure_and_bounds_timeouts(deployment_module, agent_root, monkeypatch, capsys):
    instance = deployment(deployment_module, agent_root)
    captured = []
    processes = []

    class Process:
        def __init__(self, command, **kwargs):
            captured.append((command, kwargs))
            self.stdout = io.StringIO(PUBLIC_TEST_KEY + "\n")
            self.returncode, self.expired = 0, False
            processes.append(self)

        def communicate(self, *, timeout):
            assert timeout == 10
            if self.expired:
                raise subprocess.TimeoutExpired("docker", timeout)
            return PUBLIC_TEST_KEY, None

        def wait(self, *, timeout):
            assert timeout == 10
            return self.returncode

    monkeypatch.setattr(deployment_module.subprocess, "Popen", Process)
    command = instance.compose_command("build", "migrate")
    instance._run(command, timeout=10)
    assert deployment_module.MODEL_KEY not in captured[-1][1]["env"]
    assert captured[-1][1]["cwd"] == agent_root.resolve()
    assert captured[-1][1].get("shell", False) is False
    instance._run(instance.compose_command("config", "--quiet", runtime=True), runtime=True, timeout=10)
    assert captured[-1][1]["env"][deployment_module.MODEL_KEY] == PUBLIC_TEST_KEY
    instance._run(command, timeout=10, capture=False)
    assert PUBLIC_TEST_KEY not in capsys.readouterr().out
    instance._docker_connection = ["--context", "desktop-linux"]
    instance._run(["docker", "info"], timeout=10)
    assert captured[-1][0][:3] == ["docker", "--context", "desktop-linux"]

    def failing_process(command, **kwargs):
        process = Process(command, **kwargs)
        process.returncode = 1
        return process

    monkeypatch.setattr(deployment_module.subprocess, "Popen", failing_process)
    with pytest.raises(deployment_module.DeploymentError) as failed:
        instance._run(command, timeout=10)
    assert PUBLIC_TEST_KEY not in str(failed.value)

    def expired_process(command, **kwargs):
        process = Process(command, **kwargs)
        process.expired = True
        return process

    terminated = []
    monkeypatch.setattr(deployment_module.subprocess, "Popen", expired_process)
    monkeypatch.setattr(instance, "_terminate", terminated.append)
    with pytest.raises(deployment_module.DeploymentError, match="10 秒"):
        instance._run(["docker", "version"], timeout=10)
    assert terminated == [processes[-1]] and processes[-1].stdout.closed
    assert PUBLIC_TEST_KEY not in capsys.readouterr().out


def test_cli_check_and_local_mode_use_no_key_and_start_no_application(deployment_module, agent_root, monkeypatch, capsys):
    instance = deployment(deployment_module, agent_root, model="local")
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    monkeypatch.setattr(deployment_module, "BgeDemoDeployment", lambda options: instance)
    monkeypatch.setattr(deployment_module.getpass, "getpass", lambda *args: pytest.fail("local mode must never request a key"))
    assert deployment_module.main(["--check", "--model", "local"]) == 0
    assert mutating_calls(harness) == []
    assert all(MODEL_OVERLAY not in compose_overlays(call["command"]) for call in harness.calls)
    assert deployment_module.MODEL_KEY not in instance.environ
    assert "未构建" in capsys.readouterr().out


@pytest.mark.parametrize("model", ["compatible", "local"])
def test_readiness_checks_api_web_runtime_provider_running_workers_and_six_pollers(deployment_module, agent_root, monkeypatch, model):
    instance = deployment(deployment_module, agent_root, model=model)
    requests, probes = [], []
    rows = [{"Service": service, "State": "running"} for service in deployment_module.APP_SERVICES]
    monkeypatch.setattr(instance, "_project_status", lambda: rows)

    def fetch(url):
        requests.append(url)
        if url.endswith("/ready"):
            return {"status": "ready"}
        return {"query_enabled": True, "query_scope": {"understanding_model": {
            "provider": "openai_compatible" if model == "compatible" else "local",
        }}}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def read_json(command, **kwargs):
        probes.append((command, kwargs))
        return {queue: {"workflow": 1, "activity": 1} for queue in deployment_module.QUEUES}

    monkeypatch.setattr(instance, "_fetch_json", fetch)
    monkeypatch.setattr(instance, "_read_json", read_json)
    monkeypatch.setattr(instance._http, "open", lambda url, **kwargs: requests.append(url) or Response())
    instance._wait_until_ready(timeout=1)
    assert requests == ["http://127.0.0.1:28060/ready", "http://127.0.0.1:5178/healthz",
                        "http://127.0.0.1:5178/api/v1/conversations/runtime"]
    assert len(probes) == 1 and probes[0][0][-5:-1] == ["-T", "api", "python", "-c"]
    assert probes[0][1]["timeout"] == 25
    rows[1]["State"] = "exited"
    with pytest.raises(deployment_module.DeploymentError, match="已退出"):
        instance._wait_until_ready(timeout=1)


def test_wrong_runtime_provider_never_reports_ready(deployment_module, agent_root, monkeypatch):
    instance = deployment(deployment_module, agent_root)
    monkeypatch.setattr(instance, "_project_status", lambda: [
        {"Service": service, "State": "running"} for service in deployment_module.APP_SERVICES
    ])
    monkeypatch.setattr(instance, "_fetch_json", lambda url: {"status": "ready"} if url.endswith("/ready") else {
        "query_enabled": True, "query_scope": {"understanding_model": {"provider": "local"}},
    })
    response = SimpleNamespace(status=200)

    class Open:
        def __enter__(self):
            return response

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(instance._http, "open", lambda *args, **kwargs: Open())
    with pytest.raises(deployment_module.DeploymentError, match="查询理解模型"):
        instance._wait_until_ready(timeout=1)


@pytest.mark.asyncio
async def test_poller_probe_keywords_match_real_local_sdk_without_connecting(deployment_module):
    from temporalio.client import Client

    client = await Client.connect("temporal:7233", namespace="default", lazy=True)
    signature = inspect.signature(client.workflow_service.describe_task_queue)
    probe = ast.parse(deployment_module.POLLER_PROBE)
    describe = next(node for node in ast.walk(probe) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == "describe_task_queue")
    arguments = {keyword.arg for keyword in describe.keywords}
    assert arguments == {"timeout"}
    assert "timeout" in signature.parameters and "rpc_timeout" not in arguments


def test_poller_probe_counts_only_recent_workflow_and_activity_pollers(deployment_module, monkeypatch, capsys):
    calls = []
    now = datetime.now(timezone.utc)
    ages = (0, 89, 91, -4, -6)

    async def describe(request, *, timeout):
        calls.append((request.task_queue.name, request.task_queue_type))
        assert request.namespace == "default" and timeout == timedelta(seconds=3)
        return SimpleNamespace(pollers=[SimpleNamespace(last_access_time=SimpleNamespace(
            ToDatetime=lambda *, tzinfo, age=age: now - timedelta(seconds=age),
        )) for age in ages])

    class Client:
        @staticmethod
        async def connect(endpoint, *, namespace):
            assert endpoint == "temporal:7233" and namespace == "default"
            return SimpleNamespace(workflow_service=SimpleNamespace(describe_task_queue=describe))

    monkeypatch.setitem(sys.modules, "temporalio.client", SimpleNamespace(Client=Client))
    monkeypatch.setitem(sys.modules, "temporalio.api.workflowservice.v1", SimpleNamespace(
        DescribeTaskQueueRequest=lambda **values: SimpleNamespace(**values),
    ))
    monkeypatch.setitem(sys.modules, "temporalio.api.taskqueue.v1", SimpleNamespace(
        TaskQueue=lambda **values: SimpleNamespace(**values),
    ))
    exec(deployment_module.POLLER_PROBE, {})
    counts = json.loads(capsys.readouterr().out)
    assert counts == {queue: {"workflow": 3, "activity": 3} for queue in deployment_module.QUEUES}
    assert calls == [(queue, kind) for queue in deployment_module.QUEUES for kind in (1, 2)]


@pytest.mark.parametrize("action", ["deploy", "status", "stop"])
def test_remote_docker_host_override_is_rejected_for_every_action(deployment_module, agent_root, monkeypatch, action):
    instance = deployment(deployment_module, agent_root, model="local")
    instance.environ["DOCKER_HOST"] = "tcp://198.51.100.42:2376"
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    with pytest.raises(deployment_module.DeploymentError, match="本地 Docker"):
        getattr(instance, action)()
    assert mutating_calls(harness) == []
    assert all("ps" not in call["command"] for call in harness.calls)


def test_explicit_context_has_precedence_over_host_and_is_pinned_after_validation(deployment_module, agent_root, monkeypatch):
    instance = deployment(deployment_module, agent_root, model="local")
    instance.environ.update(DOCKER_CONTEXT="desktop-linux", DOCKER_HOST="tcp://198.51.100.42:2376")
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    instance.check()
    assert instance._docker_connection == ["--context", "desktop-linux"]
    assert "desktop-linux" in next(call["command"] for call in harness.calls if "inspect" in call["command"])
    assert "DOCKER_HOST" not in instance._environment(False) and "DOCKER_CONTEXT" not in instance._environment(False)
    harness.endpoint = "tcp://198.51.100.42:2376"
    instance.environ["DOCKER_HOST"] = "unix:///fixture/local.sock"
    with pytest.raises(deployment_module.DeploymentError, match="本地 Docker"):
        instance.check()
    assert mutating_calls(harness) == []


def test_build_environment_cannot_select_external_compose_project_or_remote_builder(deployment_module, agent_root):
    instance = deployment_module.BgeDemoDeployment(
        deployment_module.DeploymentOptions(agent_root=agent_root),
        environ={"COMPOSE_PROJECT_NAME": "another-project", "COMPOSE_FILE": "/untrusted/config.yml",
                 "COMPOSE_ENV_FILES": "/untrusted/credentials", "DOCKER_HOST": "tcp://remote.example:2376",
                 "DOCKER_CONTEXT": "remote", "BUILDX_BUILDER": "remote-builder",
                 "BUILDKIT_HOST": "tcp://remote.example:1234", deployment_module.MODEL_KEY: PUBLIC_TEST_KEY},
    )
    environment = instance._environment(False)
    assert not any(name.startswith("COMPOSE_") for name in environment)
    assert "DOCKER_HOST" not in environment and "DOCKER_CONTEXT" not in environment
    assert environment.get("BUILDX_BUILDER") in (None, "default") and "BUILDKIT_HOST" not in environment
    assert deployment_module.MODEL_KEY not in environment


def test_failed_infrastructure_health_prevents_migration_and_retains_started_dependencies(deployment_module, agent_root, monkeypatch):
    instance = deployment(deployment_module, agent_root, model="local")
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    harness.rows = [{"Service": service, "State": "running", "Health": "healthy"}
                    for service in deployment_module.INFRA_SERVICES if service != "minio-init"]
    harness.rows.append({"Service": "minio-init", "State": "exited", "ExitCode": 1})
    monkeypatch.setattr(instance, "_wait_for_infrastructure",
                        deployment_module.BgeDemoDeployment._wait_for_infrastructure.__get__(instance))
    with pytest.raises(deployment_module.DeploymentError, match="minio-init"):
        instance.deploy()
    commands = [call["command"] for call in harness.calls]
    assert len([command for command in commands if "up" in command]) == 1
    assert all("run" not in command and "stop" not in command and "down" not in command for command in commands)


@pytest.mark.parametrize("kind", ["workflow", "activity"])
def test_missing_worker_pollers_block_readiness(deployment_module, agent_root, monkeypatch, kind):
    instance = deployment(deployment_module, agent_root, model="local")
    counts = {queue: {"workflow": 1, "activity": 1} for queue in deployment_module.QUEUES}
    counts["hot-news"][kind] = 0
    commands = []

    def read_json(command, **kwargs):
        commands.append(command)
        return counts

    monkeypatch.setattr(instance, "_read_json", read_json)
    assert instance._verify_pollers() is False
    assert len(commands) == 1 and "exec" in commands[0] and commands[0][-1] == deployment_module.POLLER_PROBE


def test_compose_minimum_supported_version_and_ndjson_status_are_accepted(deployment_module, agent_root, monkeypatch):
    instance = deployment(deployment_module, agent_root, model="local")
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    harness.version = "2.24.4"
    instance.check()
    assert mutating_calls(harness) == []
    monkeypatch.setattr(instance, "_run", lambda *args, **kwargs: '{"Service":"api"}\n{"Service":"web"}\n')
    assert instance._project_status() == [{"Service": "api"}, {"Service": "web"}]
    monkeypatch.setattr(instance, "_run", lambda *args, **kwargs: PUBLIC_TEST_KEY + " invalid-json")
    with pytest.raises(deployment_module.DeploymentError) as failure:
        instance._project_status()
    assert PUBLIC_TEST_KEY not in str(failure.value)


@pytest.mark.parametrize("failure", ["eof", "unsafe-terminal"])
def test_unavailable_secure_key_input_stops_before_build_without_echo_fallback(deployment_module, agent_root, monkeypatch, capsys, failure):
    instance = deployment(deployment_module, agent_root)
    instance.environ.pop(deployment_module.MODEL_KEY)
    harness = DockerHarness(deployment_module, instance, monkeypatch)
    fallback = []
    monkeypatch.setattr(deployment_module.sys.stdin, "isatty", lambda: True)

    def prompt(*args):
        if failure == "eof":
            raise EOFError("fixture terminal disconnected")
        warnings.warn("fixture cannot hide input", deployment_module.getpass.GetPassWarning)
        fallback.append("would-read-visible-input")
        return PUBLIC_TEST_KEY

    monkeypatch.setattr(deployment_module.getpass, "getpass", prompt)
    with pytest.raises(deployment_module.DeploymentError, match="安全密钥输入"):
        instance.deploy()
    assert fallback == [] and mutating_calls(harness) == []
    assert deployment_module.MODEL_KEY not in instance.environ
    assert PUBLIC_TEST_KEY not in capsys.readouterr().out


@pytest.mark.parametrize("model", ["compatible", "local"])
def test_compose_commands_bind_one_project_and_disable_implicit_env_files(deployment_module, agent_root, model):
    instance = deployment(deployment_module, agent_root, model=model)
    build = instance.compose_command("build", "--progress", "plain", "migrate", "web")
    runtime = instance.compose_command("config", "--quiet", runtime=True)
    assert build[:2] == runtime[:2] == ["docker", "compose"]
    assert "--env-file" in build and build[build.index("--env-file") + 1] == "/dev/null"
    project_option = "--project-name" if "--project-name" in build else "-p"
    assert build[build.index(project_option) + 1] == PROJECT
    assert compose_overlays(build) == list(BASE_OVERLAYS)
    assert compose_overlays(runtime) == [*BASE_OVERLAYS, *([MODEL_OVERLAY] if model == "compatible" else [])]
    assert build[-5:] == ["build", "--progress", "plain", "migrate", "web"]
    assert PUBLIC_TEST_KEY not in " ".join([*build, *runtime])


class ComposeLoader(yaml.SafeLoader):
    pass


def compose_override(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


ComposeLoader.add_constructor("!override", compose_override)
ComposeLoader.add_constructor("!reset", compose_override)


def read_compose(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeLoader)


def test_demo_compose_keeps_bge_index_mounted_read_only_and_exposes_only_local_ports():
    bge = read_compose(AGENT_ROOT / "deploy/docker-compose.bge-snapshot.yml")
    demo = read_compose(AGENT_ROOT / "deploy/docker-compose.bge-demo.yml")
    for service in ("api", "hot-news-worker"):
        projection = bge["services"][service]
        assert projection["environment"]["KNOWLEDGE_SEARCH_CONFIG_PATH"] == "/app/deploy/knowledge-search.bge-snapshot.yml"
        assert "../../rag-recall-demo:/rag-recall-demo:ro" in projection["volumes"]
        assert "./knowledge-search.bge-snapshot.yml:/app/deploy/knowledge-search.bge-snapshot.yml:ro" in projection["volumes"]
    assert str(bge["services"]["api"]["environment"]["CONVERSATION_KNOWLEDGE_ENABLED"]).lower() in {"true", "1"}
    assert "web" in demo["services"] and "data-loop-worker" in demo["services"]
    for service in demo["services"].values():
        for port in service.get("ports", []):
            if isinstance(port, str):
                assert port.startswith("127.0.0.1:"), port
            else:
                assert port.get("host_ip") == "127.0.0.1", port
    assert "8000" in str(demo["services"]["api"]["ports"])
    assert "8080" in str(demo["services"]["web"]["ports"])


def test_local_gateway_replaces_client_identity_and_preserves_one_sse_request():
    config = (AGENT_ROOT / "deploy/bge-demo.nginx.conf").read_text(encoding="utf-8")
    runtime = json.loads((AGENT_ROOT / "deploy/bge-demo.frontend.json").read_text(encoding="utf-8"))
    headers = dict(re.findall(r'proxy_set_header\s+([\w-]+)\s+"([^"\n]*)";', config))
    assert headers["Authorization"] == "Bearer newsagent-native-local-token-not-for-production"
    assert headers["X-Tenant-ID"] == "11111111-1111-4111-8111-111111111111"
    assert headers["X-User-ID"] == "22222222-2222-4222-8222-222222222222"
    assert headers["X-Hot-News-Roles"] == "hot-news:admin"
    assert headers["X-Data-Loop-Roles"] == "data-loop:admin"
    assert headers["X-Writing-Roles"] == "writing:admin"
    assert headers["X-Memory-Roles"] == "memory:read,memory:write"
    assert "memory:approve" not in headers["X-Memory-Roles"]
    assert all(headers[name] == "" for name in ("X-Team-ID", "X-Section-ID", "X-Role-ID"))
    assert "http://api:8000" in config and "resolver 127.0.0.11" in config
    for directive in ("proxy_next_upstream off;", "proxy_next_upstream_tries 1;",
                      "proxy_buffering off;", "proxy_request_buffering off;", "proxy_cache off;"):
        assert directive in config
    assert "location = /healthz" in config and 'return 200 "ok\\n";' in config
    assert runtime["apiBaseUrl"] == runtime["tenantId"] == ""
    assert runtime["environment"] == "e2e" and runtime["enableHotNews"] is True
    assert "NEWSAGENT_MODEL_API_KEY" not in config and PUBLIC_TEST_KEY not in config
