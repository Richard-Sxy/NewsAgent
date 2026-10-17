"""The public local starter fixes model mode without deployment on import."""

import importlib.util
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_bge_demo_deploy import DockerHarness, MODEL_OVERLAY, PUBLIC_TEST_KEY, agent_root, mutating_calls


SCRIPT = Path(__file__).resolve().parents[1] / "tools/start_local_demo.py"


def load_starter(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    name = "newsagent_start_local_demo_test_module"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def starter(monkeypatch):
    return load_starter(monkeypatch)


@pytest.mark.parametrize("arguments", [
    [], ["--check"], ["status"], ["stop"],
    ["--skip-build", "--api-port", "28061", "--web-port", "5179"],
    ["deploy", "--min-free-gib", "12"], ["--model-profile", "fixture"],
])
def test_forwards_actions_and_options_with_local_mode_fixed_last(starter, monkeypatch, arguments):
    calls = []
    monkeypatch.setattr(starter, "deployment_main", lambda args: calls.append(args) or 0)
    original = list(arguments)
    assert starter.main(arguments) == 0
    assert calls == [[*original, "--model", "local"]]
    assert arguments == original


def test_default_argv_uses_command_line_but_explicit_empty_argv_stays_empty(starter, monkeypatch):
    calls = []
    monkeypatch.setattr(starter, "deployment_main", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "status", "--api-port", "28061"])
    assert starter.main() == 0
    assert starter.main([]) == 0
    assert calls == [["status", "--api-port", "28061", "--model", "local"], ["--model", "local"]]


@pytest.mark.parametrize("arguments", [
    ["--model", "compatible"], ["--model=compatible"],
    ["--model", "local"], ["--model=local"],
])
def test_rejects_model_overrides_before_calling_deployment(starter, monkeypatch, capsys, arguments):
    monkeypatch.setattr(starter, "deployment_main", lambda args: pytest.fail("model override must not reach deployment"))
    assert starter.main(arguments) == 2
    output = capsys.readouterr()
    assert output.out == "" and "固定" in output.err and ("local" in output.err or "本地" in output.err)


@pytest.mark.parametrize("exit_code", [1, 130])
def test_preserves_deployment_exit_status(starter, monkeypatch, exit_code):
    monkeypatch.setattr(starter, "deployment_main", lambda args: exit_code)
    assert starter.main([]) == exit_code


def test_import_does_not_start_processes_or_ask_for_model_keys(monkeypatch):
    import getpass

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("import must not execute deployment"))
    monkeypatch.setattr(getpass, "getpass", lambda *args, **kwargs: pytest.fail("import must not request a model key"))
    module = load_starter(monkeypatch)
    assert callable(module.main) and callable(module.deployment_main)


def test_actual_local_check_needs_no_enterprise_config_or_key(starter, agent_root, monkeypatch):
    backend = sys.modules[starter.deployment_main.__module__]
    constructor = backend.BgeDemoDeployment
    instances, harnesses = [], []
    (agent_root / "deploy/model-runtime.compatible.yml").unlink()
    (agent_root / "deploy/docker-compose.timeline-query-model.yml").unlink()

    def create(options):
        assert options.model == "local"
        instance = constructor(replace(options, agent_root=agent_root), environ={})
        instances.append(instance)
        harnesses.append(DockerHarness(backend, instance, monkeypatch))
        return instance

    monkeypatch.setattr(backend, "BgeDemoDeployment", create)
    monkeypatch.setattr(backend.getpass, "getpass", lambda *args: pytest.fail("local check cannot require a model key"))
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path.name != "model-runtime.compatible.yml" and not path.name.startswith(".env")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    assert starter.main(["--check"]) == 0
    assert len(instances) == 1 and backend.MODEL_KEY not in instances[0].environ
    assert mutating_calls(harnesses[0]) == []
    assert all(MODEL_OVERLAY not in str(call["command"]) for call in harnesses[0].calls)


def test_cli_help_is_available_without_docker_or_project_working_directory(tmp_path):
    response = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], cwd=tmp_path,
        env={"PATH": os.defpath}, capture_output=True, text=True, timeout=10,
    )
    assert response.returncode == 0 and response.stderr == ""
    assert "usage:" in response.stdout.lower() and "--api-port" in response.stdout and "--skip-build" in response.stdout
    assert "--model" not in response.stdout and "无需企业模型密钥" in response.stdout
    assert "[1/6" not in response.stdout and "部署停止" not in response.stdout


def test_local_deployment_removes_inherited_model_key_from_every_process_environment(starter, agent_root, monkeypatch, capsys):
    backend = sys.modules[starter.deployment_main.__module__]
    constructor = backend.BgeDemoDeployment
    instances = []

    def create(options):
        assert options.model == "local"
        instance = constructor(replace(options, agent_root=agent_root), environ={backend.MODEL_KEY: PUBLIC_TEST_KEY})
        instances.append(instance)
        DockerHarness(backend, instance, monkeypatch)
        return instance

    monkeypatch.setattr(backend, "BgeDemoDeployment", create)
    monkeypatch.setattr(backend.getpass, "getpass", lambda *args: pytest.fail("local deployment cannot request a model key"))
    assert starter.main([]) == 0
    assert len(instances) == 1
    for runtime in (False, True):
        environment = instances[0]._environment(runtime)
        assert backend.MODEL_KEY not in environment and PUBLIC_TEST_KEY not in environment.values()
    assert PUBLIC_TEST_KEY not in capsys.readouterr().out
