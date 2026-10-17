"""The query-only activation cannot recreate storage or expose credentials."""

import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def module(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "tools/activate_structured_query_demo.py"
    monkeypatch.syspath_prepend(str(script.parent))
    spec = importlib.util.spec_from_file_location("query_activation_test", script)
    value = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, value)
    spec.loader.exec_module(value)
    return value


def test_activation_only_recreates_api_after_key_and_config_validation(module, monkeypatch):
    calls = []
    class Deployment:
        def __init__(self, options):
            assert options.model == "compatible"
            calls.append("init")
        def _ensure_model_key(self): calls.append("key")
        def compose_command(self, *, runtime):
            assert runtime
            return ["docker", "compose", "--env-file", "/dev/null", "-p", "news-agent-bge-demo"]
        def _run(self, command, **options):
            assert options["runtime"]
            calls.append(command)
        def _wait_until_ready(self): calls.append("ready")
    monkeypatch.setattr(module, "BgeDemoDeployment", Deployment)
    assert module.main() == 0
    assert calls[:2] == ["init", "key"] and calls[-1] == "ready"
    assert calls[2][-2:] == ["config", "--quiet"]
    assert calls[3][-8:] == ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "api"]
    assert any(arg.endswith("docker-compose.structured-query-demo.yml") for arg in calls[3])


def test_missing_key_stops_before_any_deployment(module, monkeypatch, capsys):
    class Deployment:
        def __init__(self, options): pass
        def _ensure_model_key(self): raise module.DeploymentError("missing key")
        def _redact(self, message): return message
        def compose_command(self, **kwargs): pytest.fail("must not deploy")
    monkeypatch.setattr(module, "BgeDemoDeployment", Deployment)
    assert module.main() == 1
    assert "missing key" in capsys.readouterr().err
