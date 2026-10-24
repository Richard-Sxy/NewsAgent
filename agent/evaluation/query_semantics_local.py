"""Local Transformers/PEFT inference only; no downloads, training, or HTTP server.

Heavy dependencies are lazy. Generation uses a soft max_time budget: a generation
step can exceed it. Hashing and model loading precede the prediction budget.
"""
from __future__ import annotations

from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
import platform
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.model_runtime.config_file import ModelRuntimeConfig
from app.model_runtime.core import RawInferenceResult, StructuredInferenceService, StructuredOutputError
from app.modules.conversation.query_understanding import QueryUnderstanding
from app.modules.conversation.structured_query_resolution import resolve_structured_query
from evaluation.query_semantics_common import canonical_sha, load_package, read_json, sha256_file
from evaluation.query_semantics_runner import _payload, _safe_usage, _write_new


class LocalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_path: str
    tokenizer_path: str
    adapter_path: str | None = None
    training_manifest_path: str | None = None
    base_model: str = "tencent/Hunyuan-7B-Instruct"
    dtype: Literal["float16", "bfloat16", "float32"]
    device_map: Literal["auto", "cpu", "cuda"]
    max_new_tokens: int = Field(ge=1, le=8192)


def _stream_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _assets(directory: str, *, kind: str) -> tuple[Path, dict, dict]:
    root = Path(directory).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("model/tokenizer/adapter must be existing local directories")
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix in {
        ".json", ".safetensors", ".bin", ".model", ".txt", ".tiktoken", ".jinja"})
    if kind == "tokenizer":
        files = [path for path in files if path.suffix not in {".safetensors", ".bin"}
                 and not path.name.endswith(".index.json")
                 and path.name not in {"config.json", "generation_config.json", "adapter_config.json"}]
    elif kind == "model":
        files = [path for path in files if path.name in {"config.json", "generation_config.json"}
                 or path.suffix in {".safetensors", ".bin"} or path.name.endswith(".index.json")]
    else:
        files = [path for path in files if path.name in {
            "adapter_config.json", "adapter_model.safetensors", "adapter_model.bin"}]
    if not files:
        raise ValueError("no local model assets found")
    if kind == "model":
        if not (root / "config.json").is_file() or (root / "adapter_config.json").exists():
            raise ValueError("base model must have config.json and no adapter_config.json")
        if not any(path.suffix in {".safetensors", ".bin"} for path in files):
            raise ValueError("base model weights missing")
        if read_json(root / "config.json").get("quantization_config"):
            raise ValueError("this local runner supports unquantized Transformers weights only")
    if kind == "adapter" and (not (root / "adapter_config.json").is_file()
                               or not any(path.name in {"adapter_model.safetensors", "adapter_model.bin"} for path in files)):
        raise ValueError("local PEFT adapter config/weights missing")
    for index in (root.glob("*.index.json") if kind == "model" else []):
        for value in set(read_json(index).get("weight_map", {}).values()):
            shard = root / value
            if Path(value).is_absolute() or ".." in Path(value).parts or not shard.is_file() or shard not in files:
                raise ValueError("weight index points outside the frozen local shard set")
    hashes = {str(path.relative_to(root)): _stream_hash(path) for path in files}
    stamps = {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in files}
    return root, hashes, stamps


def _training(config: LocalConfig, case_groups: set[str]) -> tuple[list[str], dict | None]:
    if config.adapter_path is None:
        if config.training_manifest_path is not None:
            raise ValueError("base evaluation cannot declare task training")
        return [], None
    if not config.training_manifest_path:
        raise ValueError("LoRA evaluation requires the approved exported training manifest")
    path = Path(config.training_manifest_path).expanduser().resolve(strict=True)
    manifest = read_json(path)
    groups = manifest.get("group_ids")
    if (manifest.get("human_review") != "approved" or manifest.get("loss_scope_required") != "assistant_only"
            or not isinstance(groups, list) or not groups
            or not all(isinstance(group, str) and group.strip() for group in groups) or len(set(groups)) != len(groups)
            or manifest.get("messages_sha256") != sha256_file(path.parent / "train.messages.jsonl")):
        raise ValueError("training manifest is unapproved, incomplete, or changed")
    if case_groups & set(groups):
        raise ValueError("training groups overlap evaluation groups")
    return sorted(groups), {"manifest_sha256": sha256_file(path), "messages_sha256": manifest["messages_sha256"],
                            "provenance": "reviewed_input_ledger_not_proof_of_adapter_training"}


def _unchanged(stamps: dict) -> bool:
    try:
        return all((Path(path).stat().st_size, Path(path).stat().st_mtime_ns) == stamp for path, stamp in stamps.items())
    except OSError:
        return False


class TransformersInferencePort:
    def __init__(self, *, model, tokenizer, torch, max_new_tokens: int):
        self.model, self.tokenizer, self.torch = model, tokenizer, torch
        self.max_new_tokens = max_new_tokens

    async def complete(self, request):
        messages = [{"role": item.role, "content": item.content} for item in request.messages]
        rendered = self.tokenizer.apply_chat_template(messages, tokenize=False,
            enable_thinking=False, add_generation_prompt=True)
        inputs = self.tokenizer(rendered, return_tensors="pt", add_special_tokens=False, truncation=False)
        length = inputs["input_ids"].shape[-1]
        context_limit = getattr(self.model.config, "max_position_embeddings", None)
        if isinstance(context_limit, int) and length + self.max_new_tokens > context_limit:
            raise ValueError("input_plus_output_exceeds_context_without_truncation")
        device = self.model.get_input_embeddings().weight.device
        inputs = inputs.to(device)
        with self.torch.inference_mode():
            generated = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens,
                do_sample=False, max_time=request.timeout_seconds, num_beams=1)
        tail = generated[0][length:]
        return RawInferenceResult(content=self.tokenizer.decode(tail, skip_special_tokens=True),
            model_version=request.model_route, usage={"prompt_tokens": length,
                "completion_tokens": len(tail), "total_tokens": length + len(tail)})


def _load_port(config: LocalConfig, model_path: Path, tokenizer_path: Path, adapter_path: Path | None):
    import torch
    import transformers
    from packaging.version import Version
    if Version(transformers.__version__) < Version("4.56.0"):
        raise ValueError("Hunyuan local inference requires transformers >= 4.56.0")
    tokenizer = transformers.AutoTokenizer.from_pretrained(str(tokenizer_path),
        local_files_only=True, trust_remote_code=False)
    if not getattr(tokenizer, "chat_template", None):
        raise ValueError("the local tokenizer has no explicit chat template")
    model = transformers.AutoModelForCausalLM.from_pretrained(str(model_path),
        local_files_only=True, trust_remote_code=False, torch_dtype=getattr(torch, config.dtype),
        device_map=config.device_map)
    if adapter_path is not None:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, str(adapter_path), is_trainable=False,
                                          local_files_only=True, trust_remote_code=False)
    model.eval()
    dependencies = {name: version(name) for name in ("torch", "transformers", "accelerate", "peft")}
    return TransformersInferencePort(model=model, tokenizer=tokenizer, torch=torch,
        max_new_tokens=config.max_new_tokens), canonical_sha(tokenizer.chat_template), dependencies


async def run_local_predictions(package: Path, config_path: Path, out: Path,
                                max_calls: int, max_seconds: float) -> dict:
    if type(max_calls) is not int or max_calls < 0 or type(max_seconds) not in {int, float} or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("invalid prediction budget")
    package, out = Path(package), Path(out)
    manifest, cases, _ = load_package(package, with_gold=False)
    config = LocalConfig.model_validate(read_json(Path(config_path)))
    runtime = ModelRuntimeConfig.model_validate(read_json(package / "runtime.json"))
    binding = runtime.agent_scene("query_understanding")
    if len(runtime.agent_scenes) != 1 or len(runtime.prompts) != 1:
        raise ValueError("package runtime must contain query_understanding only")
    if out.exists():
        raise FileExistsError("output already exists")
    groups, training = _training(config, {item["group_id"] for item in cases})
    model_path, model_files, stamps = _assets(config.model_path, kind="model")
    tokenizer_path, tokenizer_files, tokenizer_stamps = _assets(config.tokenizer_path, kind="tokenizer")
    adapter_path, adapter_files = None, None
    stamps.update(tokenizer_stamps)
    if config.adapter_path:
        adapter_path, adapter_files, adapter_stamps = _assets(config.adapter_path, kind="adapter")
        stamps.update(adapter_stamps)
        metadata = read_json(adapter_path / "adapter_config.json")
        if metadata.get("peft_type") != "LORA" or metadata.get("base_model_name_or_path") not in {
            config.base_model, str(model_path), config.model_path}:
            raise ValueError("adapter must be LoRA bound to the declared local base model")
    try:
        port, template_sha, dependencies = _load_port(config, model_path, tokenizer_path, adapter_path)
    except (ImportError, PackageNotFoundError):
        raise ValueError("local inference requires torch, transformers >= 4.56.0, accelerate, peft and packaging") from None
    if not _unchanged(stamps):
        raise ValueError("local model assets changed while loading")
    identity = {"role": "lora" if adapter_path else "base", "base_model": config.base_model,
        "base_revision": canonical_sha(model_files), "tokenizer_revision": canonical_sha(tokenizer_files),
        "chat_template_sha256": template_sha, "quantization": "none:" + config.dtype,
        "adapter_sha256": canonical_sha(adapter_files) if adapter_files else None, "training_group_ids": groups}
    settings = {"backend": "transformers_local", "dtype": config.dtype, "device_map": config.device_map,
        "max_new_tokens": config.max_new_tokens, "do_sample": False, "enable_thinking": False,
        "num_beams": 1, "truncation": False, "timeout_kind": "generate_max_time_soft"}
    out.mkdir(parents=True, exist_ok=False)
    _write_new(out / "run-spec.json", {"schema_version": 1, "run_id": out.name,
        "scope": "offline_query_understanding_no_warehouse", "package_manifest_sha256": sha256_file(package / "manifest.json"),
        "runtime_sha256": sha256_file(package / "runtime.json"), "identity": identity, "endpoint_settings": settings,
        "source_files_sha256": manifest["source_files_sha256"],
        "dependency_versions": {"python": platform.python_version(), **dependencies,
            **{name: version(name) for name in ("pydantic", "httpx", "PyYAML", "opentelemetry-api")}},
        "budget": {"max_calls": max_calls, "max_seconds": max_seconds, "concurrency": 1, "attempts_per_case": 1,
                   "timeout_kind": "soft_between_generation_steps", "excludes": ["hashing", "model_loading"]},
        "case_ids": [item["id"] for item in cases], "planned": len(cases),
        "input_sha256": {item["id"]: canonical_sha(item) for item in cases},
        "local_assets": {"model": model_files, "tokenizer": tokenizer_files, "adapter": adapter_files},
        "training": training, "identity_verification": "local_asset_content_hashes",
        "production_acceptance": "not_approved", "training_started": False})
    service = StructuredInferenceService(inference=port, prompts=runtime.prompt_registry())
    started, calls, rows, stop = monotonic(), 0, [], None
    with (out / "predictions.jsonl").open("x", encoding="utf-8") as handle:
        for case in cases:
            row = {key: case[key] for key in ("id", "group_id", "split", "cohort")}
            row.update(status="not_run", attempts=0, proposal=None, resolved=None, error_kind=None,
                       elapsed_ms=0.0, usage={}, request_id=None, raw_sha256=None)
            remaining = max_seconds - (monotonic() - started)
            stop = stop or ("call_budget_exhausted" if calls >= max_calls else "wall_budget_exhausted" if remaining <= 0 else None)
            if stop:
                row["error_kind"] = stop
            else:
                before = monotonic()
                try:
                    payload, policy, clock = _payload(case)
                    calls += 1
                    row["attempts"] = 1
                    result = await service.run(tenant_id="query-evaluation", trace_id=case["id"],
                        idempotency_key=out.name + ":" + case["id"], scene="query_understanding",
                        prompt_version=binding.prompt_version, model_route="local-hunyuan-evaluation",
                        payload=payload, output_type=QueryUnderstanding, timeout_seconds=remaining)
                    row.update(status="ok", proposal=result.value.model_dump(mode="json"),
                        resolved=resolve_structured_query(payload["question"], result.value, policy, now=clock).model_dump(mode="json"),
                        usage=_safe_usage(dict(result.usage)), raw_sha256=sha256(result.raw_content.encode()).hexdigest())
                except Exception as exc:
                    kind = "invalid_output" if isinstance(exc, StructuredOutputError) else "runner_error"
                    row.update(status="error", error_kind=kind)
                    if kind == "runner_error":
                        stop = kind
                row["elapsed_ms"] = round((monotonic() - before) * 1000, 3)
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            rows.append(row)
    if not _unchanged(stamps):
        stop = "runner_error"
    completion = {"schema_version": 1, "run_id": out.name,
        "status": "completed" if stop is None and all(row["status"] == "ok" for row in rows) else "incomplete",
        "planned": len(cases), "completed": sum(row["status"] == "ok" for row in rows),
        "errors": sum(row["status"] == "error" for row in rows), "not_run": sum(row["status"] == "not_run" for row in rows),
        "real_inference_calls": calls, "stop_reason": stop, "elapsed_ms": round((monotonic() - started) * 1000, 3),
        "predictions_sha256": sha256_file(out / "predictions.jsonl"),
        "model_quality": "not_run", "production_acceptance": "not_approved",
        "limitations": ["Local generation uses a soft time budget; a generation step can overrun it.",
                        "Loading/hashing are outside prediction budget; no GPU quality run is implied by offline tests."]}
    _write_new(out / "completion.json", completion)
    return completion
