"""Contract-only fake-weight/fake-module tests; no GPU or quality evidence."""
from contextlib import nullcontext
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import InferenceMessage, InferenceRequest, RawInferenceResult
from evaluation import query_semantics_local as local


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    package, model = tmp_path / "package", tmp_path / "model"
    package.mkdir()
    model.mkdir()
    write(model / "config.json", {"model_type": "hunyuan_v1_dense"})
    write(model / "tokenizer_config.json", {"chat_template": "fake local template"})
    (model / "model.safetensors").write_bytes(b"fake contract weights; not a usable model")
    config = {"model_path": str(model), "tokenizer_path": str(model), "adapter_path": None,
              "training_manifest_path": None, "dtype": "bfloat16", "device_map": "cpu", "max_new_tokens": 128}
    config_path = tmp_path / "local.json"
    write(config_path, config)
    original = load_model_runtime_config("deploy/model-runtime.local.yml")
    binding = original.agent_scene("query_understanding")
    runtime = original.model_dump(mode="json")
    runtime["agent_scenes"] = [binding.model_dump(mode="json")]
    runtime["prompts"] = [p.model_dump(mode="json") for p in original.prompts
                          if p.scene == binding.scene and p.version == binding.prompt_version]
    write(package / "runtime.json", runtime)
    manifest = {"source_files_sha256": {"evaluation/query_semantics_local.py":
                                      sha256(Path(local.__file__).read_bytes()).hexdigest()}}
    write(package / "manifest.json", manifest)
    policy = {"scenario": {"id": "news-ranking", "name": "排行", "description": "test",
                           "allowed_sort_metrics": ["clicks", "ctr"], "sample_questions": ["点击量前5条"]},
              "default_limit": 5, "max_limit": 100,
              "supported_window_start": "2026-10-09T08:00:00+08:00",
              "supported_window_end": "2026-10-09T09:00:00+08:00"}
    cases = [{"id": f"Q{i}", "group_id": f"G{i}", "split": "test", "cohort": "golden",
              "question": "点击量前5条新闻", "now": "2026-10-10T12:00:00+08:00", "policy": policy}
             for i in range(3)]
    def load(path, *, with_gold):
        assert with_gold is False
        return manifest, deepcopy(cases), None
    monkeypatch.setattr(local, "load_package", load)
    return package, config_path, config, cases, model


class FakePort:
    def __init__(self, content=None):
        self.requests = []
        self.content = content or json.dumps({"status": "ready", "intent": {
            "sort_by": "clicks", "row_limit": 5, "explanation": "保留原意"}})
    async def complete(self, request):
        self.requests.append(request)
        return RawInferenceResult(content=self.content, model_version=request.model_route)


def fake_load(monkeypatch, port):
    monkeypatch.setattr(local, "_load_port", lambda *args: (
        port, sha256(b"fake template").hexdigest(), {"torch": "fake", "transformers": "fake", "peft": "fake"}))


@pytest.mark.asyncio
async def test_local_native_structured_chain_hashes_and_budget(setup, tmp_path, monkeypatch):
    package, config_path, _, _, _ = setup
    port = FakePort()
    fake_load(monkeypatch, port)
    result = await local.run_local_predictions(package, config_path, tmp_path / "run", 1, 30)
    assert result["completed"] == result["real_inference_calls"] == 1
    assert result["not_run"] == 2 and result["model_quality"] == "not_run"
    spec = json.loads((tmp_path / "run/run-spec.json").read_text())
    assert spec["identity"]["role"] == "base" and spec["identity"]["adapter_sha256"] is None
    assert spec["identity"]["base_revision"] == local.canonical_sha(spec["local_assets"]["model"])
    assert "model.safetensors" not in spec["local_assets"]["tokenizer"]
    assert spec["endpoint_settings"]["timeout_kind"] == "generate_max_time_soft"
    rows = [json.loads(line) for line in (tmp_path / "run/predictions.jsonl").read_text().splitlines()]
    assert rows[0]["resolved"]["requested_window_start"] == "2026-10-09T08:00:00+08:00"
    assert rows[1]["attempts"] == 0
    with pytest.raises(FileExistsError):
        await local.run_local_predictions(package, config_path, tmp_path / "run", 1, 30)


def add_adapter(tmp_path, config, config_path, *, group="TRAIN1"):
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    write(adapter / "adapter_config.json", {"peft_type": "LORA", "base_model_name_or_path": "tencent/Hunyuan-7B-Instruct"})
    (adapter / "adapter_model.safetensors").write_bytes(b"fake adapter")
    messages = tmp_path / "train.messages.jsonl"
    messages.write_text('{"messages": []}\n')
    training = {"human_review": "approved", "loss_scope_required": "assistant_only", "group_ids": [group],
                "messages_sha256": sha256(messages.read_bytes()).hexdigest()}
    path = tmp_path / "training-manifest.json"
    write(path, training)
    config.update(adapter_path=str(adapter), training_manifest_path=str(path))
    write(config_path, config)
    return adapter, path


@pytest.mark.asyncio
async def test_lora_uses_reviewed_ledger_and_actual_adapter_hash(setup, tmp_path, monkeypatch):
    package, config_path, config, _, _ = setup
    add_adapter(tmp_path, config, config_path)
    fake_load(monkeypatch, FakePort())
    await local.run_local_predictions(package, config_path, tmp_path / "run", 3, 30)
    spec = json.loads((tmp_path / "run/run-spec.json").read_text())
    assert spec["identity"]["role"] == "lora" and spec["identity"]["training_group_ids"] == ["TRAIN1"]
    assert spec["identity"]["adapter_sha256"] == local.canonical_sha(spec["local_assets"]["adapter"])
    assert "not_proof" in spec["training"]["provenance"]


@pytest.mark.parametrize("problem", ["overlap", "changed_messages", "wrong_base"])
@pytest.mark.asyncio
async def test_bad_training_provenance_blocks_before_load(setup, tmp_path, monkeypatch, problem):
    package, config_path, config, _, _ = setup
    adapter, _ = add_adapter(tmp_path, config, config_path, group="G1" if problem == "overlap" else "TRAIN1")
    if problem == "changed_messages":
        (tmp_path / "train.messages.jsonl").write_text("changed")
    if problem == "wrong_base":
        write(adapter / "adapter_config.json", {"peft_type": "LORA", "base_model_name_or_path": "remote/wrong-base"})
    monkeypatch.setattr(local, "_load_port", lambda *a: pytest.fail("must not load invalid artifacts"))
    with pytest.raises(ValueError):
        await local.run_local_predictions(package, config_path, tmp_path / "run", 3, 30)
    assert not (tmp_path / "run").exists()


@pytest.mark.asyncio
async def test_invalid_json_keeps_failures_and_never_reflects_raw(setup, tmp_path, monkeypatch):
    package, config_path, _, _, _ = setup
    port = FakePort("private-invalid-output")
    fake_load(monkeypatch, port)
    result = await local.run_local_predictions(package, config_path, tmp_path / "run", 2, 30)
    assert result["errors"] == len(port.requests) == 2 and result["not_run"] == 1
    assert "private-invalid-output" not in (tmp_path / "run/predictions.jsonl").read_text()


@pytest.mark.asyncio
async def test_runtime_failure_or_mutated_weights_blocks_quality(setup, tmp_path, monkeypatch):
    package, config_path, _, _, model_path = setup
    class FailingPort(FakePort):
        async def complete(self, request):
            raise RuntimeError("private error details")
    fake_load(monkeypatch, FailingPort())
    result = await local.run_local_predictions(package, config_path, tmp_path / "failed", 3, 30)
    assert result["stop_reason"] == "runner_error" and result["not_run"] == 2
    assert "private error details" not in (tmp_path / "failed/predictions.jsonl").read_text()
    class MutatingPort(FakePort):
        async def complete(self, request):
            (model_path / "model.safetensors").write_bytes(b"changed during inference")
            return await super().complete(request)
    fake_load(monkeypatch, MutatingPort())
    result = await local.run_local_predictions(package, config_path, tmp_path / "drift", 3, 30)
    assert result["completed"] == 3 and result["status"] == "incomplete"
    assert result["stop_reason"] == "runner_error"


@pytest.mark.asyncio
async def test_missing_heavy_dependencies_produces_actionable_error(setup, tmp_path, monkeypatch):
    package, config_path, _, _, _ = setup
    def missing(*args):
        raise ModuleNotFoundError("no torch")
    monkeypatch.setattr(local, "_load_port", missing)
    with pytest.raises(ValueError, match="requires torch"):
        await local.run_local_predictions(package, config_path, tmp_path / "run", 3, 30)
    assert not (tmp_path / "run").exists()


def test_asset_hash_changes_and_index_escape_rejected(setup):
    *_, model = setup
    _, before, _ = local._assets(str(model), kind="model")
    (model / "model.safetensors").write_bytes(b"changed contract weights")
    _, after, _ = local._assets(str(model), kind="model")
    assert local.canonical_sha(before) != local.canonical_sha(after)
    write(model / "model.safetensors.index.json", {"weight_map": {"x": "../outside.safetensors"}})
    with pytest.raises(ValueError, match="shard set"):
        local._assets(str(model), kind="model")


@pytest.mark.asyncio
async def test_lazy_transformers_loader_and_generation_contract(setup, monkeypatch):
    *_, model_path = setup
    calls = {}
    class Inputs(dict):
        def to(self, device):
            calls["device"] = device
            return self
    class Tokenizer:
        chat_template = "actual-fake-template"
        def apply_chat_template(self, messages, **kwargs):
            calls["template"] = kwargs
            return "rendered"
        def __call__(self, text, **kwargs):
            calls["tokenize"] = kwargs
            return Inputs(input_ids=SimpleNamespace(shape=(1, 3)))
        def decode(self, tail, **kwargs):
            assert tail == [4, 5]
            return "{}"
    class Model:
        config = SimpleNamespace(max_position_embeddings=1000)
        def eval(self):
            calls["eval"] = True
        def get_input_embeddings(self):
            return SimpleNamespace(weight=SimpleNamespace(device="cpu"))
        def generate(self, **kwargs):
            calls["generate"] = kwargs
            return [[1, 2, 3, 4, 5]]
    def tokenizer_loader(path, **kwargs):
        calls["tokenizer_loader"] = kwargs
        return Tokenizer()
    def model_loader(path, **kwargs):
        calls["model_loader"] = kwargs
        return Model()
    def peft_loader(model, path, **kwargs):
        calls["peft_loader"] = kwargs
        return model
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(bfloat16="bf16", inference_mode=nullcontext))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(__version__="4.56.0",
        AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer_loader),
        AutoModelForCausalLM=SimpleNamespace(from_pretrained=model_loader)))
    monkeypatch.setitem(sys.modules, "peft", SimpleNamespace(PeftModel=SimpleNamespace(from_pretrained=peft_loader)))
    monkeypatch.setattr(local, "version", lambda name: "fake-contract-version")
    cfg = local.LocalConfig(model_path=str(model_path), tokenizer_path=str(model_path),
        dtype="bfloat16", device_map="cpu", max_new_tokens=20)
    port, template_sha, _ = local._load_port(cfg, model_path, model_path, model_path)
    request = InferenceRequest(tenant_id="test", trace_id="test", idempotency_key="test", scene="query_understanding",
        prompt_version="v5", model_route="local", messages=(InferenceMessage("system", "schema"),),
        output_json_schema={}, timeout_seconds=2)
    value = await port.complete(request)
    assert value.content == "{}" and value.usage["completion_tokens"] == 2
    assert template_sha == local.canonical_sha("actual-fake-template")
    for key in ("model_loader", "tokenizer_loader", "peft_loader"):
        assert calls[key]["local_files_only"] is True and calls[key]["trust_remote_code"] is False
    assert calls["peft_loader"]["is_trainable"] is False
    assert calls["template"]["enable_thinking"] is False
    assert calls["tokenize"]["truncation"] is False
    assert calls["generate"]["max_time"] == 2 and calls["generate"]["do_sample"] is False
    port.model.config.max_position_embeddings = 5
    with pytest.raises(ValueError, match="without_truncation"):
        await port.complete(request)
