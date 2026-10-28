"""Offline contracts for a pinned local writing profile; never load a model."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

from app.model_runtime import config_file
from evaluation import run_report_pilot as cli


def local_config(**overrides):
    raw = config_file.load_model_runtime_config(cli.DEFAULT_CONFIG).model_dump(mode="json")
    raw["inference"].update(
        provider="openai_compatible", url=cli.LOCAL_WRITING_URL,
        api_key_env=cli.LOCAL_WRITING_KEY_ENV, model_routes=[cli.LOCAL_WRITING_MODEL],
        response_format="none", extra_body={"max_tokens": 4096},
    )
    for scene in raw["agent_scenes"]:
        scene["model_route"] = cli.LOCAL_WRITING_MODEL
    raw["inference"].update(overrides)
    return config_file.ModelRuntimeConfig.model_validate(raw)


def safe_mock(monkeypatch, config):
    monkeypatch.setattr(config_file, "load_model_runtime_config", lambda path: config)
    return cli.safe_config(cli.DEFAULT_CONFIG.with_name("synthetic-local-profile-test.yml"))


def test_default_deepseek_remains_accepted():
    config = cli.safe_config(cli.DEFAULT_CONFIG)
    assert str(config.inference.url) == "https://api.deepseek.com/v1/chat/completions"
    assert config.inference.api_key_env == "DEEPSEEK_API_KEY"
    assert not cli._local_writing_profile(config)


def test_exact_local_profile_is_accepted(monkeypatch):
    config = safe_mock(monkeypatch, local_config())
    assert cli._local_writing_profile(config)
    assert config.inference.response_format == "none"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8019/v1/chat/completions",
    "http://localhost:8018/v1/chat/completions",
    "http://0.0.0.0:8018/v1/chat/completions",
    "http://127.0.0.1:8018/v1/chat/completions?target=remote",
    "https://example.com/v1/chat/completions",
])
def test_other_destinations_never_use_local_shortcut(monkeypatch, url):
    with pytest.raises(ValueError, match="fixed loopback"):
        safe_mock(monkeypatch, local_config(url=url))


@pytest.mark.parametrize("override", [
    {"api_key_env": "DEEPSEEK_API_KEY"},
    {"response_format": "json_object"},
    {"response_format": "json_schema"},
    {"model_routes": [cli.LOCAL_WRITING_MODEL, "other-model"]},
])
def test_local_identity_and_output_contract_are_fixed(monkeypatch, override):
    with pytest.raises(ValueError, match="fixed loopback"):
        safe_mock(monkeypatch, local_config(**override))


def test_per_scene_model_substitution_is_rejected(monkeypatch):
    raw = local_config().model_dump(mode="json")
    raw["inference"]["model_routes"].append("other-model")
    raw["agent_scenes"][0]["model_route"] = "other-model"
    with pytest.raises(ValueError, match="fixed loopback"):
        safe_mock(monkeypatch, config_file.ModelRuntimeConfig.model_validate(raw))


@pytest.mark.parametrize("extra", [
    {"max_tokens": 4096, "thinking": {"type": "disabled"}},
    {"max_tokens": 8193},
    {"max_tokens": True},
    {},
])
def test_local_body_is_bounded_and_has_no_provider_specific_fields(monkeypatch, extra):
    with pytest.raises(ValueError):
        safe_mock(monkeypatch, local_config(extra_body=extra))


def test_local_key_does_not_read_any_environment_or_prompt(monkeypatch):
    config = local_config()
    def forbidden(*args, **kwargs):
        raise AssertionError("local profile must never request a secret")
    monkeypatch.setattr(cli.os.environ, "get", forbidden)
    monkeypatch.setattr(cli.getpass, "getpass", forbidden)
    assert cli._execution_api_key(config) == "local-only"


def test_deepseek_still_uses_its_existing_environment_key(monkeypatch):
    config = config_file.load_model_runtime_config(cli.DEFAULT_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    assert cli._execution_api_key(config) == "synthetic-test-key"


def test_deepseek_noninteractive_missing_key_still_blocks(monkeypatch):
    config = config_file.load_model_runtime_config(cli.DEFAULT_CONFIG)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        cli._execution_api_key(config)


def test_local_config_still_requires_public_evaluation_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(config_file, "load_model_runtime_config", lambda path: local_config())
    with pytest.raises(ValueError, match="public credential-free"):
        cli.safe_config(tmp_path / "outside.yml")


def test_main_local_route_uses_real_http_execution_branch(monkeypatch, tmp_path):
    config = local_config()
    run = tmp_path / "synthetic-run"
    spec = {"config": {"path": str(cli.DEFAULT_CONFIG)}}
    monkeypatch.setattr(cli, "verify", lambda path, for_execution: spec)
    monkeypatch.setattr(cli, "safe_config", lambda path: config)
    called = []
    async def fake_execute(path, key, **kwargs):
        called.append((path, key, kwargs))
        return {"status": "synthetic-test-only"}
    monkeypatch.setattr(cli, "execute", fake_execute)
    monkeypatch.setattr(sys, "argv", ["run_report_pilot", "run", "--run-dir", str(run)])
    cli.main()
    assert called == [(run.resolve(), "local-only", {})]
