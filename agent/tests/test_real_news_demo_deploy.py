"""Offline contracts for isolated real-content deployment; no Docker or network."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml


AGENT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = AGENT_ROOT / "tools/start_real_news_demo.py"


@pytest.fixture(scope="module")
def module():
    name = "newsagent_real_news_deployment_tests"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    yield loaded
    sys.modules.pop(name, None)


@pytest.fixture
def source_root(tmp_path, module):
    root = tmp_path / "agent"
    names = [*(f"deploy/{name}" for name in module.OVERLAYS),
        "deploy/text2sql-scenes.corpus-v6.yml", "deploy/conversation-hooks.model-runtime.local.yml",
        "deploy/knowledge-search.bge-snapshot.yml", "deploy/bge-demo.nginx.conf",
        "deploy/bge-demo.frontend.json", "frontend/Dockerfile.conversation-hooks.cached",
        "frontend/dist/index.html"]
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    for directory in ("examples", "alembic", "tests"):
        (root / directory).mkdir()
    catalog = root / module.CATALOG
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"articles": [{"news_id": str(i)} for i in range(120)]}))
    schema = root / module.SCHEMA
    schema.parent.mkdir()
    schema.write_text("frozen-v6-contract\n")
    app = root / "app/sql_assistant"
    app.mkdir(parents=True)
    (app / "corpus_profiles.py").write_text(
        f"CORPUS_CATALOG_SHA256 = {hashlib.sha256(catalog.read_bytes()).hexdigest()!r}\n")
    (app / "warehouse.py").write_text(
        f"SCHEMA_SHA256_V6 = {hashlib.sha256(schema.read_bytes()).hexdigest()!r}\n")
    for name in ("SHA256SUMS", "data/experiments/offline_recall_fg/chunks.jsonl",
        "data/experiments/offline_recall_fg/embeddings.npy", "data/experiments/offline_recall_fg/index.bin",
        "data/models/bge-large-zh-v1.5/modules.json"):
        path = root.parent / "rag-recall-demo" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("immutable fixture\n")
    return root


def deployment(module, root, **kwargs):
    return module.RealNewsDemoDeployment(module.DeploymentOptions(
        agent_root=root, model="local", api_port=28070, web_port=5179, **kwargs), environ={})


def test_incremental_index_is_an_explicit_last_overlay_and_requires_built_package(module, source_root):
    instance = module.RealNewsDemoDeployment(module.DeploymentOptions(
        agent_root=source_root, model="local", api_port=28070, web_port=5179, skip_build=True),
        environ={}, incremental_news_index=True)
    command = instance.compose_command("up", "-d", "api")
    assert command[-5:] == ["-f", str(source_root / "deploy" / module.INCREMENTAL_OVERLAY), "up", "-d", "api"]
    with pytest.raises(module.DeploymentError, match="incremental"):
        instance._validate_files()
    for name in (f"deploy/{module.INCREMENTAL_OVERLAY}", f"deploy/{module.INCREMENTAL_CONFIG}",
                 f"{module.INCREMENTAL_PACKAGE}/SHA256SUMS", f"{module.INCREMENTAL_PACKAGE}/build_report.json"):
        path = source_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    instance._validate_files()
    assert module.argument_parser().parse_args(["--incremental-news-index"]).incremental_news_index is True


def test_bulk_index_uses_a_separate_version_without_replacing_v1(module, source_root):
    instance = module.RealNewsDemoDeployment(module.DeploymentOptions(
        agent_root=source_root, model="local", api_port=28070, web_port=5179, skip_build=True),
        environ={}, incremental_news_index=True, incremental_news_version="v2")
    command = instance.compose_command("up", "-d", "api")
    assert command[-5:] == ["-f", str(source_root / "deploy/docker-compose.bulk-news-demo.yml"), "up", "-d", "api"]
    assert instance.index_package.endswith("-v2")
    with pytest.raises(module.DeploymentError, match="v2"):
        instance._validate_files()
    assert module.INCREMENTAL_PACKAGE.endswith("-v1")
    assert module.argument_parser().parse_args(["--incremental-news-version", "v2"]).incremental_news_version == "v2"


class Harness:
    def __init__(self, module, instance, monkeypatch):
        self.error = module.DeploymentError
        self.calls = []
        self.endpoint = "unix:///fixture/docker.sock"
        self.rows = []
        self.fail_on = None
        self.ports = []
        monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=50 * module.GIB))
        monkeypatch.setattr(module.shutil, "which", lambda *a, **k: "/fixture/docker")
        harness = self

        class Probe:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def bind(self, pair):
                harness.ports.append(pair)

        monkeypatch.setattr(module.socket, "socket", lambda *a: Probe())
        monkeypatch.setattr(instance, "_run", self.run)
        monkeypatch.setattr(instance, "_wait_for_infrastructure", lambda: self.calls.append(["infrastructure-ready"]))
        monkeypatch.setattr(instance, "_wait_until_ready", lambda: self.calls.append(["application-ready"]))

    def run(self, command, **kwargs):
        command = list(command)
        self.calls.append(command)
        if self.fail_on and self.fail_on(command):
            raise self.error("fixture failure")
        if command[:3] == ["docker", "context", "show"]:
            return "desktop-linux"
        if command[:3] == ["docker", "context", "inspect"]:
            return json.dumps(self.endpoint)
        if command[:3] == ["docker", "compose", "version"]:
            return "2.39.4"
        if command[:2] == ["docker", "info"]:
            return str(8 * 1024 ** 3)
        if "ps" in command:
            return json.dumps(self.rows)
        return ""


def test_commands_fix_new_project_disable_dotenv_and_keep_all_five_overlays(module, source_root):
    instance = deployment(module, source_root)
    command = instance.compose_command("config", "--quiet", runtime=True)
    assert command[:6] == ["docker", "compose", "--env-file", "/dev/null", "-p", "news-agent-real-news-demo"]
    overlays = [Path(command[i + 1]).name for i, arg in enumerate(command) if arg == "-f"]
    assert overlays == list(module.OVERLAYS)
    assert "timeline-query-model" not in " ".join(command)


def test_check_reads_only_frozen_files_and_local_image_ids(module, source_root, monkeypatch, capsys):
    instance = deployment(module, source_root)
    harness = Harness(module, instance, monkeypatch)
    instance.environ.update(COMPOSE_PROJECT_NAME="old", NEWSAGENT_MODEL_API_KEY="fixture-model-key")
    (source_root / ".env").write_text("must never be read")
    instance.check()
    assert not any(arg in call for call in harness.calls for arg in ("up", "run", "build", "stop", "down"))
    inspected = [call[3] for call in harness.calls if call[:3] == ["docker", "image", "inspect"]]
    assert inspected == [module.APP_IMAGE, module.WEB_RUNTIME_IMAGE]
    assert harness.ports == [("127.0.0.1", 28070), ("127.0.0.1", 5179)]
    output = capsys.readouterr().out
    assert "news-agent-real-news-demo" in output and "fixture-model-key" not in output


@pytest.mark.parametrize("relative", ["data/corpus-news-v6/catalog.json", "docs/sql-warehouse-schema-v6.md"])
def test_changed_frozen_file_blocks_before_docker(module, source_root, monkeypatch, relative):
    instance = deployment(module, source_root)
    harness = Harness(module, instance, monkeypatch)
    (source_root / relative).write_text("changed")
    with pytest.raises(module.DeploymentError, match="SHA-256"):
        instance.check()
    assert harness.calls == []


def test_wrong_catalog_count_is_rejected_even_with_matching_hash(module, source_root):
    catalog = source_root / module.CATALOG
    catalog.write_text(json.dumps({"articles": []}))
    (source_root / "app/sql_assistant/corpus_profiles.py").write_text(
        f"CORPUS_CATALOG_SHA256 = {hashlib.sha256(catalog.read_bytes()).hexdigest()!r}\n")
    with pytest.raises(module.DeploymentError, match="120"):
        deployment(module, source_root)._validate_files()


@pytest.mark.parametrize("skip_build", [False, True])
def test_deploy_reuses_app_image_builds_only_dist_and_orders_migration(module, source_root, monkeypatch, skip_build):
    instance = deployment(module, source_root, skip_build=skip_build)
    harness = Harness(module, instance, monkeypatch)
    instance.deploy()
    builds = [c for c in harness.calls if "build" in c]
    assert len(builds) == (0 if skip_build else 1)
    if builds:
        assert builds[0][-2:] == ["build", "web"]
    verify = next(c for c in harness.calls if c[:2] == ["docker", "run"])
    assert "--network" in verify and verify[verify.index("--network") + 1] == "none"
    assert "--read-only" in verify and verify[-3] == module.APP_IMAGE
    mounts = [verify[i + 1] for i, arg in enumerate(verify) if arg == "--mount"]
    assert len(mounts) == 3 and all(m.endswith(",readonly") for m in mounts)
    infra = next(c for c in harness.calls if "up" in c)
    migration = next(c for c in harness.calls if "run" in c and "migrate" in c)
    apps = [c for c in harness.calls if "up" in c][-1]
    assert harness.calls.index(infra) < harness.calls.index(["infrastructure-ready"]) < harness.calls.index(migration)
    assert harness.calls.index(migration) < harness.calls.index(apps) < harness.calls.index(["application-ready"])
    assert apps[-len(module.APP_SERVICES):] == list(module.APP_SERVICES)
    assert all("--no-build" in c for c in (infra, apps))
    assert "--build" not in migration  # compose run uses the fixed local image; build is reset in the overlay
    assert all(c[c.index("--pull") + 1] == "never" for c in (infra, migration, apps))
    inspected = [c[3] for c in harness.calls if c[:3] == ["docker", "image", "inspect"]]
    assert inspected == [module.APP_IMAGE, module.WEB_IMAGE if skip_build else module.WEB_RUNTIME_IMAGE]


def test_failed_migration_is_not_retried_and_never_starts_applications(module, source_root, monkeypatch):
    instance = deployment(module, source_root)
    harness = Harness(module, instance, monkeypatch)
    harness.fail_on = lambda c: "run" in c and "migrate" in c
    with pytest.raises(module.DeploymentError, match="fixture failure"):
        instance.deploy()
    assert len([c for c in harness.calls if "run" in c and "migrate" in c]) == 1
    assert len([c for c in harness.calls if "up" in c]) == 1
    assert not any("stop" in c or "down" in c for c in harness.calls)


@pytest.mark.parametrize("action", ["check", "status", "stop"])
def test_remote_daemon_rejected_before_any_project_operation(module, source_root, monkeypatch, action):
    instance = deployment(module, source_root)
    instance.environ["DOCKER_HOST"] = "tcp://198.51.100.10:2376"
    harness = Harness(module, instance, monkeypatch)
    with pytest.raises(module.DeploymentError, match="本地 Docker"):
        getattr(instance, action)()
    assert not any(arg in c for c in harness.calls for arg in ("ps", "up", "run", "build", "stop"))


def test_occupied_new_port_is_allowed_only_for_this_project(module, source_root, monkeypatch):
    instance = deployment(module, source_root)
    harness = Harness(module, instance, monkeypatch)
    harness.rows = [{"Project": module.PROJECT, "Service": "api", "State": "running",
        "Publishers": [{"URL": "127.0.0.1", "PublishedPort": 28070}]}]
    instance._check_ports()
    assert harness.ports == [("127.0.0.1", 5179)]
    harness.ports.clear()
    harness.rows[0]["Project"] = "news-agent-bge-demo"
    instance._check_ports()
    assert harness.ports == [("127.0.0.1", 28070), ("127.0.0.1", 5179)]


def test_status_whitelists_public_fields_stop_preserves_volumes(module, source_root, monkeypatch, capsys):
    instance = deployment(module, source_root)
    harness = Harness(module, instance, monkeypatch)
    harness.rows = [{"Service": "api", "State": "running", "Health": "healthy", "Config.Env": "do not print"}]
    instance.status()
    assert "Config.Env" not in capsys.readouterr().out
    instance.stop()
    stops = [c for c in harness.calls if "stop" in c]
    assert len(stops) == 1 and "news-agent-real-news-demo" in stops[0]
    assert not any("down" in c or "--volumes" in c for c in harness.calls)


def test_cli_flags_keep_local_only_defaults(module):
    parser = module.argument_parser()
    args = parser.parse_args(["--status"])
    assert args.api_port == 28070 and args.web_port == 5179 and args.status
    assert parser.parse_args(["--stop"]).stop
    assert parser.parse_args(["--check", "--skip-build"]).skip_build
    with pytest.raises(SystemExit):
        parser.parse_args(["--model", "compatible"])


class ComposeLoader(yaml.SafeLoader):
    pass


ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node, deep=True))
ComposeLoader.add_constructor("!reset", lambda loader, node: None)


def test_overlay_keeps_current_sources_readonly_fixed_profile_and_offline_dist_build():
    config = yaml.load((AGENT_ROOT / "deploy/docker-compose.real-news-demo.yml").read_text(), Loader=ComposeLoader)
    assert config["name"] == "news-agent-real-news-demo"
    assert "volumes" not in config and "networks" not in config  # no external/shared names
    services = config["services"]
    for name in ("migrate", "api", "hot-news-worker", "writing-worker", "outbox-worker", "data-loop-worker", "test-runner"):
        service = services[name]
        assert service["image"] == "news-agent/conversation-hooks-bge-demo:local"
        assert service["pull_policy"] == "never"
        env = service["environment"]
        assert env["SQL_ASSISTANT_DATASET_PROFILE"] == "corpus-v6"
        assert env["SQL_ASSISTANT_NEWS_PER_TENANT"] == "120"
        assert env["SQL_ASSISTANT_SCENARIOS_PATH"].endswith("text2sql-scenes.corpus-v6.yml")
        assert env["MODEL_RUNTIME_CONFIG_PATH"].endswith("conversation-hooks.model-runtime.local.yml")
        for directory in ("app", "examples", "docs", "data"):
            assert f"../{directory}:/app/{directory}:ro" in service["volumes"]
        assert all(v.endswith(":ro") for v in service["volumes"])
    assert services["migrate"]["build"] is None
    assert "../alembic:/app/alembic:ro" in services["migrate"]["volumes"]
    assert "../tests:/app/tests:ro" in services["test-runner"]["volumes"]
    web = services["web"]
    assert web["build"] == {"context": "../frontend/dist", "dockerfile": "../Dockerfile.conversation-hooks.cached",
        "network": "none", "args": {"WEB_RUNTIME_IMAGE": "news-agent/frontend:conversation-hooks-demo"}}
    assert web["ports"] == ["127.0.0.1:${NEWSAGENT_REAL_WEB_PORT:-5179}:8080"]
    assert services["api"]["ports"] == ["127.0.0.1:${NEWSAGENT_REAL_API_PORT:-28070}:8000"]
