"""Contract-only tests; MockTransport outputs are not model-quality evidence."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import httpx
import pytest

from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.http import OpenAICompatibleInferenceClient
from evaluation import query_semantics_runner as runner


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    source = Path(runner.__file__)
    manifest = {"source_files_sha256": {
        "evaluation/query_semantics_runner.py": sha256(source.read_bytes()).hexdigest()}}
    write(package / "manifest.json", manifest)
    source_config = load_model_runtime_config("deploy/model-runtime.local.yml")
    config = source_config.model_dump(mode="json")
    binding = source_config.agent_scene("query_understanding")
    config["agent_scenes"] = [binding.model_dump(mode="json")]
    config["prompts"] = [item.model_dump(mode="json") for item in source_config.prompts
                         if item.scene == binding.scene and item.version == binding.prompt_version]
    write(package / "runtime.json", config)
    policy = {
        "scenario": {"id": "news-ranking", "name": "新闻排行", "description": "批准的排行",
                     "allowed_sort_metrics": ["clicks", "ctr", "hot_score"],
                     "sample_questions": ["点击量前5条新闻"]},
        "default_limit": 5, "max_limit": 100,
        "supported_window_start": "2026-10-09T08:00:00+08:00",
        "supported_window_end": "2026-10-09T09:00:00+08:00",
        "coverage_window_start": "2026-10-09T00:00:00+08:00",
        "coverage_window_end": "2026-10-10T00:00:00+08:00",
        "data_watermark": "2026-10-10T00:00:00+08:00",
    }
    cases = [{"id": f"Q{i}", "group_id": f"G{i}", "split": "test", "cohort": "golden",
              "question": "昨天八点到九点科技视频按点击率前五条", "now": "2026-10-10T12:00:00+08:00",
              "policy": policy} for i in range(3)]
    calls = []

    def load(path, *, with_gold):
        calls.append(with_gold)
        assert not with_gold, "prediction must not load labels"
        return manifest, deepcopy(cases), None

    monkeypatch.setattr(runner, "load_package", load)
    endpoint = {
        "base_url": "http://127.0.0.1:8000/v1", "model_route": "test-hunyuan",
        "api_key_env": "QUERY_EVAL_TEST_API_KEY", "timeout_seconds": 3,
        "response_format": "json_object", "extra_body": {"max_tokens": 512},
        "identity": {"base_model": "org/hunyuan-7b", "base_revision": "fixed-commit-20261024",
                     "tokenizer_revision": "fixed-tokenizer-20261024",
                     "chat_template_sha256": sha256(b"test chat template").hexdigest(),
                     "quantization": "bf16", "adapter_sha256": None,
                     "role": "base", "training_group_ids": []},
    }
    endpoint_path = tmp_path / "endpoint.json"
    write(endpoint_path, endpoint)
    monkeypatch.setenv("QUERY_EVAL_TEST_API_KEY", "secret-for-transport-test")
    return package, endpoint_path, endpoint, cases, calls


def proposal():
    return {"status": "ready", "time_expression": "yesterday", "explicit_date": None,
            "hour_start": 8, "hour_end": 9, "time_basis": "behavior", "ranking_source": "unspecified",
            "response_style": "list", "unsupported_conditions": [],
            "intent": {"sort_by": "ctr", "row_limit": 5, "category": "科技", "content_type": "video",
                       "sort_direction": "desc", "explanation": "查询已给定条件"}}


def install_transport(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(runner, "OpenAICompatibleInferenceClient",
                        lambda config, *, api_key: OpenAICompatibleInferenceClient(
                            config, api_key=api_key, client=client))
    return client


def response(value=None, *, model="test-hunyuan"):
    return httpx.Response(200, json={"id": "request-contract-test", "model": model,
                                   "choices": [{"message": {"content": json.dumps(value or proposal())}}],
                                   "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}})


@pytest.mark.asyncio
async def test_real_adapter_schema_resolution_and_gold_isolation(bundle, tmp_path, monkeypatch):
    package, endpoint_path, _, _, loads = bundle
    received = []

    def handler(request):
        received.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer secret-for-transport-test"
        assert str(request.url).endswith("/v1/chat/completions")
        return response()

    client = install_transport(monkeypatch, handler)
    try:
        result = await runner.run_predictions(package, endpoint_path, tmp_path / "run", 3, 30)
    finally:
        await client.aclose()
    assert result["completed"] == result["real_inference_calls"] == 3
    assert result["model_quality"] == "not_run" and loads == [False]
    rows = [json.loads(line) for line in (tmp_path / "run/predictions.jsonl").read_text().splitlines()]
    assert all(row["attempts"] == 1 and row["status"] == "ok" for row in rows)
    assert rows[0]["proposal"]["ranking_source"] == "unspecified"
    assert rows[0]["resolved"]["ranking_source"] == "project_computed"
    assert rows[0]["resolved"]["requested_window_start"] == "2026-10-09T08:00:00+08:00"
    sent = json.loads(received[0]["messages"][1]["content"])
    assert sent["temporal_context"]["date_without_year"] == "current_year"
    assert "gold" not in json.dumps(received)
    assert received[0]["temperature"] == 0
    assert result["predictions_sha256"] == sha256((tmp_path / "run/predictions.jsonl").read_bytes()).hexdigest()
    assert "secret-for-transport-test" not in (tmp_path / "run/run-spec.json").read_text()


@pytest.mark.asyncio
async def test_budget_retains_unattempted_cases_and_does_not_overwrite(bundle, tmp_path, monkeypatch):
    package, endpoint_path, _, _, _ = bundle
    count = []
    client = install_transport(monkeypatch, lambda request: count.append(1) or response())
    try:
        result = await runner.run_predictions(package, endpoint_path, tmp_path / "run", 1, 30)
        with pytest.raises(FileExistsError):
            await runner.run_predictions(package, endpoint_path, tmp_path / "run", 1, 30)
    finally:
        await client.aclose()
    assert len(count) == 1 and result["not_run"] == 2 and result["planned"] == 3


@pytest.mark.asyncio
async def test_identity_mismatch_hard_stops_and_failure_response_is_not_saved(bundle, tmp_path, monkeypatch):
    package, endpoint_path, _, _, _ = bundle
    client = install_transport(monkeypatch, lambda request: response(model="wrong-private-model"))
    try:
        result = await runner.run_predictions(package, endpoint_path, tmp_path / "run", 3, 30)
    finally:
        await client.aclose()
    text = (tmp_path / "run/predictions.jsonl").read_text()
    assert result["real_inference_calls"] == result["errors"] == 1
    assert result["not_run"] == 2 and result["stop_reason"] == "model_version_mismatch"
    assert "wrong-private-model" not in text


@pytest.mark.asyncio
async def test_invalid_output_keeps_denominator_without_retry(bundle, tmp_path, monkeypatch):
    package, endpoint_path, _, _, _ = bundle
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"model": "test-hunyuan", "choices": [
            {"message": {"content": "invalid-private-output"}}]})

    client = install_transport(monkeypatch, handler)
    try:
        result = await runner.run_predictions(package, endpoint_path, tmp_path / "run", 3, 30)
    finally:
        await client.aclose()
    assert len(calls) == result["errors"] == 3 and result["completed"] == 0
    text = (tmp_path / "run/predictions.jsonl").read_text()
    assert "invalid_output" in text and "invalid-private-output" not in text


@pytest.mark.asyncio
async def test_timeout_is_counted_once_per_case(bundle, tmp_path, monkeypatch):
    package, endpoint_path, _, _, _ = bundle

    def handler(request):
        raise httpx.ReadTimeout("secret-for-transport-test", request=request)

    client = install_transport(monkeypatch, handler)
    try:
        result = await runner.run_predictions(package, endpoint_path, tmp_path / "run", 2, 30)
    finally:
        await client.aclose()
    assert result["errors"] == 2 and result["not_run"] == 1
    assert "secret-for-transport-test" not in (tmp_path / "run/predictions.jsonl").read_text()


@pytest.mark.parametrize("change", [
    {"base_url": "http://name:secret@localhost/v1"},
    {"model_route": "REPLACE_ME"},
    {"extra_body": {"messages": []}},
])
@pytest.mark.asyncio
async def test_bad_endpoint_fails_before_output_or_network(bundle, tmp_path, change):
    package, endpoint_path, endpoint, _, _ = bundle
    endpoint.update(change)
    write(endpoint_path, endpoint)
    with pytest.raises(ValueError, match="configuration is invalid"):
        await runner.run_predictions(package, endpoint_path, tmp_path / "run", 3, 30)
    assert not (tmp_path / "run").exists()


@pytest.mark.asyncio
async def test_training_overlap_and_unpinned_identity_block_before_output(bundle, tmp_path):
    package, endpoint_path, endpoint, _, _ = bundle
    endpoint["identity"].update(role="lora", adapter_sha256=sha256(b"test adapter").hexdigest())
    endpoint["identity"]["training_group_ids"] = ["G1"]
    write(endpoint_path, endpoint)
    with pytest.raises(ValueError, match="overlap"):
        await runner.run_predictions(package, endpoint_path, tmp_path / "run", 3, 30)
    endpoint["identity"]["training_group_ids"] = []
    endpoint["identity"]["base_revision"] = "main"
    write(endpoint_path, endpoint)
    with pytest.raises(ValueError, match="configuration is invalid"):
        await runner.run_predictions(package, endpoint_path, tmp_path / "run", 3, 30)
    assert not (tmp_path / "run").exists()


@pytest.mark.asyncio
async def test_wall_budget_stops_without_network_and_keeps_all_cases(bundle, tmp_path, monkeypatch):
    package, endpoint_path, _, _, _ = bundle
    ticks = iter(range(20))
    monkeypatch.setattr(runner, "monotonic", lambda: next(ticks))

    def handler(request):
        pytest.fail("an expired global budget must not make a request")

    client = install_transport(monkeypatch, handler)
    try:
        result = await runner.run_predictions(package, endpoint_path, tmp_path / "run", 3, 0.5)
    finally:
        await client.aclose()
    assert result["real_inference_calls"] == 0 and result["not_run"] == 3
    assert result["stop_reason"] == "wall_budget_exhausted"


@pytest.mark.asyncio
async def test_source_identity_drift_fails_before_output(bundle, tmp_path, monkeypatch):
    package, endpoint_path, _, _, _ = bundle
    original = runner.load_package

    def changed(*args, **kwargs):
        manifest, cases, gold = original(*args, **kwargs)
        manifest = deepcopy(manifest)
        manifest["source_files_sha256"]["evaluation/query_semantics_runner.py"] = "0" * 64
        return manifest, cases, gold

    monkeypatch.setattr(runner, "load_package", changed)
    with pytest.raises(ValueError, match="source identity drift"):
        await runner.run_predictions(package, endpoint_path, tmp_path / "run", 3, 30)
    assert not (tmp_path / "run").exists()
