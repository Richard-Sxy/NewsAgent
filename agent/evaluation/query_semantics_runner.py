"""Portable, bounded inference for frozen query-understanding inputs.

This runner never reads gold labels, starts training, executes SQL, or changes
production configuration. All network work is behind ``run_predictions``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from hashlib import sha256
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import re
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.domain.errors import AgentOutputValidationError
from app.model_runtime.agent_client import NativeStructuredAgentClient, model_request_context
from app.model_runtime.config_file import EndpointConfig, ModelRuntimeConfig
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.http import ModelTransportError, OpenAICompatibleInferenceClient
from app.modules.conversation.query_understanding import QueryPolicy, QueryUnderstanding
from app.modules.conversation.structured_query_resolution import resolve_structured_query
from app.sql_assistant.input_boundary import normalize_question, SqlAssistantQuestionError
from evaluation.query_semantics_common import load_package, read_json, sha256_file


_AGENT_ROOT = Path(__file__).resolve().parents[1]
_PLACEHOLDER = re.compile(r"replace[-_ ]?me|change[-_ ]?me|todo|填入|待填|<[^>]+>|\$\{", re.I)
_HASH = re.compile(r"[0-9a-f]{64}")
_EXTRA_KEYS = {"max_tokens", "max_completion_tokens", "top_p", "seed", "stop", "thinking",
               "frequency_penalty", "presence_penalty", "repetition_penalty"}


class ModelIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    base_model: str = Field(min_length=1, max_length=256)
    base_revision: str = Field(min_length=1, max_length=256)
    tokenizer_revision: str = Field(min_length=1, max_length=256)
    chat_template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quantization: str = Field(min_length=1, max_length=128)
    adapter_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    role: Literal["base", "lora", "reference"]
    training_group_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def pinned(self):
        if self.role == "lora" and self.adapter_sha256 is None:
            raise ValueError("lora identity requires adapter digest")
        if self.role == "base" and self.adapter_sha256 is not None:
            raise ValueError("base identity cannot contain an adapter")
        if self.role == "base" and self.training_group_ids:
            raise ValueError("base identity cannot claim task training groups")
        if len(set(self.training_group_ids)) != len(self.training_group_ids) or any(
            not value.strip() for value in self.training_group_ids
        ):
            raise ValueError("training groups must be nonempty and unique")
        if self.role in {"base", "lora"} and any(
            value.lower() in {"main", "master", "latest", "unknown", "unverified"}
            for value in (self.base_revision, self.tokenizer_revision)
        ):
            raise ValueError("base and tokenizer identities must be pinned")
        return self


class RemoteEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    base_url: str = Field(min_length=1)
    model_route: str = Field(min_length=1, max_length=128)
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    timeout_seconds: float = Field(gt=0, le=300)
    response_format: Literal["none", "json_object", "json_schema"]
    extra_body: dict[str, Any]
    identity: ModelIdentity

    @model_validator(mode="after")
    def safe_endpoint(self):
        decoded = urlsplit(self.base_url)
        if (decoded.scheme not in {"http", "https"} or not decoded.hostname
                or decoded.username is not None or decoded.password is not None
                or decoded.query or decoded.fragment):
            raise ValueError("endpoint must be an HTTP URL without embedded credentials or query")
        if set(self.extra_body) - _EXTRA_KEYS:
            raise ValueError("unsupported inference settings")
        if not math.isfinite(self.timeout_seconds):
            raise ValueError("timeout must be finite")
        encoded = json.dumps(self.model_dump(), ensure_ascii=False, allow_nan=False)
        if _PLACEHOLDER.search(encoded):
            raise ValueError("endpoint configuration contains placeholders")
        for digest in (self.identity.chat_template_sha256, self.identity.adapter_sha256):
            if digest is not None and len(set(digest)) == 1:
                raise ValueError("identity digest appears to be a placeholder")
        return self

    @property
    def completion_url(self) -> str:
        value = self.base_url.rstrip("/")
        return value if value.endswith("/chat/completions") else value + "/chat/completions"


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             allow_nan=False).encode()).hexdigest()


def _write_new(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


def _source_hashes(manifest: dict) -> dict[str, str]:
    hashes = manifest.get("source_files_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("package source identities are missing")
    for relative, expected in hashes.items():
        path = (_AGENT_ROOT / relative).resolve()
        if (not path.is_relative_to(_AGENT_ROOT) or not path.is_file()
                or not isinstance(expected, str) or not _HASH.fullmatch(expected)
                or sha256_file(path) != expected):
            raise ValueError("package source identity drift")
    return hashes


def _runtime(package: Path, endpoint: RemoteEndpoint) -> ModelRuntimeConfig:
    baseline = ModelRuntimeConfig.model_validate(read_json(package / "runtime.json"))
    binding = baseline.agent_scene("query_understanding")
    if len(baseline.agent_scenes) != 1 or len(baseline.prompts) != 1:
        raise ValueError("package runtime must contain only query_understanding")
    data = baseline.model_dump(mode="json")
    data["inference"] = EndpointConfig(
        provider="openai_compatible", model_routes=(endpoint.model_route,),
        url=endpoint.completion_url, api_key_env=endpoint.api_key_env,
        timeout_seconds=endpoint.timeout_seconds, response_format=endpoint.response_format,
        extra_body=endpoint.extra_body,
    ).model_dump(mode="json")
    data["agent_scenes"] = [{"scene": "query_understanding", "prompt_version": binding.prompt_version,
                             "model_route": endpoint.model_route}]
    return ModelRuntimeConfig.model_validate(data)


def _payload(case: dict) -> tuple[dict, QueryPolicy, datetime]:
    policy = QueryPolicy.model_validate(case["policy"])
    clock = datetime.fromisoformat(case["now"])
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("case clock must be timezone aware")
    from zoneinfo import ZoneInfo
    clock = clock.astimezone(ZoneInfo(policy.timezone))
    question = normalize_question(case["question"])
    return {"question": question, "policy": policy.model_dump(mode="json"),
            "temporal_context": {"server_now": clock.isoformat(), "timezone": policy.timezone,
                                 "date_without_year": "current_year",
                                 "day_without_month": "current_year_and_month",
                                 "hour_convention": "24_hour"}}, policy, clock


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, AgentOutputValidationError):
        return "invalid_output"
    if isinstance(exc, ModelTransportError):
        name = getattr(exc, "failure_kind", "transport_error")
        return name if name in {"model_version_mismatch", "unapproved_model", "timeout", "connection",
                                "rate_limit", "http_error", "invalid_json", "response_too_large",
                                "response_validation"} else "transport_error"
    return "runner_error"


def _safe_usage(value: dict) -> dict:
    return {key: number for key, number in value.items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
            and type(number) is int and number >= 0}


async def run_predictions(package: Path, endpoint_path: Path, out: Path,
                          max_calls: int, max_seconds: float) -> dict:
    """Make one attempt per case; keep failed/unattempted cases in the denominator.

    Caller must explicitly run this on the machine hosting the chosen service.
    Credentials are read only from the named environment variable, never .env.
    """
    if type(max_calls) is not int or max_calls < 0:
        raise ValueError("max_calls must be a nonnegative integer")
    if type(max_seconds) not in {float, int} or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be positive and finite")
    package, endpoint_path, out = Path(package), Path(endpoint_path), Path(out)
    manifest, cases, _ = load_package(package, with_gold=False)
    sources = _source_hashes(manifest)
    try:
        endpoint = RemoteEndpoint.model_validate(read_json(endpoint_path))
    except (ValidationError, ValueError, TypeError):
        raise ValueError("endpoint configuration is invalid or unpinned") from None
    if set(endpoint.identity.training_group_ids) & {case["group_id"] for case in cases}:
        raise ValueError("training groups overlap the evaluation package")
    config = _runtime(package, endpoint)
    api_key = os.environ.get(endpoint.api_key_env, "")
    if not api_key.strip():
        raise ValueError("the configured API key environment variable is missing")
    # Validate immutable data before creating output or constructing an HTTP client.
    for case in cases:
        QueryPolicy.model_validate(case["policy"])
        clock = datetime.fromisoformat(case["now"])
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError("case clock must be timezone aware")
    out.mkdir(parents=True, exist_ok=False)
    run_id = out.name
    dependency_versions = {"python": platform.python_version(), **{
        name: version(name) for name in ("pydantic", "httpx", "PyYAML", "opentelemetry-api")}}
    settings = {"model_route": endpoint.model_route, "api_key_env": endpoint.api_key_env,
                "endpoint_url_sha256": sha256(endpoint.completion_url.encode()).hexdigest(),
                "timeout_seconds": endpoint.timeout_seconds, "response_format": endpoint.response_format,
                "extra_body": endpoint.extra_body, "temperature": 0, "stream": False}
    spec = {"schema_version": 1, "run_id": run_id,
            "scope": "offline_query_understanding_no_warehouse",
            "package_manifest_sha256": sha256_file(package / "manifest.json"),
            "runtime_sha256": sha256_file(package / "runtime.json"),
            "identity": endpoint.identity.model_dump(mode="json"), "endpoint_settings": settings,
            "dependency_versions": dependency_versions, "source_files_sha256": sources,
            "budget": {"max_calls": max_calls, "max_seconds": max_seconds, "concurrency": 1,
                       "attempts_per_case": 1},
            "case_ids": [case["id"] for case in cases], "planned": len(cases),
            "input_sha256": {case["id"]: _digest(case) for case in cases},
            "identity_verification": "declared_weights_response_model_checked",
            "production_acceptance": "not_approved", "training_started": False}
    _write_new(out / "run-spec.json", spec)
    started, calls, rows, stop = monotonic(), 0, [], None
    inference = OpenAICompatibleInferenceClient(config.inference, api_key=api_key)
    client = NativeStructuredAgentClient(
        StructuredInferenceService(inference=inference, prompts=config.prompt_registry()),
        config, timeout_seconds=endpoint.timeout_seconds,
    )
    try:
        with (out / "predictions.jsonl").open("x", encoding="utf-8") as handle:
            for case in cases:
                row = {key: case[key] for key in ("id", "group_id", "split", "cohort")}
                row.update(status="not_run", attempts=0, proposal=None, resolved=None,
                           error_kind=None, elapsed_ms=0.0, usage={}, request_id=None, raw_sha256=None)
                remaining = max_seconds - (monotonic() - started)
                if stop is None and calls >= max_calls:
                    stop = "call_budget_exhausted"
                if stop is None and remaining <= 0:
                    stop = "wall_budget_exhausted"
                if stop is not None:
                    row["error_kind"] = stop
                else:
                    before = monotonic()
                    try:
                        payload, policy, clock = _payload(case)
                        calls += 1
                        row["attempts"] = 1
                        with model_request_context(tenant_id="query-semantics-evaluation",
                                                   trace_id=f"query-eval-{uuid4()}"):
                            async with asyncio.timeout(min(remaining, endpoint.timeout_seconds)):
                                result = await client.run_structured(
                                    app_id="python:query-understanding-v5", mode="query_understanding",
                                    payload=payload, output_type=QueryUnderstanding,
                                )
                        row.update(status="ok", proposal=result.value.model_dump(mode="json"),
                                   resolved=resolve_structured_query(payload["question"], result.value,
                                       policy, now=clock).model_dump(mode="json"),
                                   usage=_safe_usage(result.usage), request_id=result.request_id,
                                   raw_sha256=sha256(result.raw_content.encode()).hexdigest())
                    except SqlAssistantQuestionError:
                        policy = QueryPolicy.model_validate(case["policy"])
                        clock = datetime.fromisoformat(case["now"])
                        proposal = QueryUnderstanding(status="unsupported", unsupported_conditions=["input_boundary"])
                        row.update(status="error", proposal=None,
                                   resolved=resolve_structured_query(case["question"], proposal, policy,
                                       now=clock).model_dump(mode="json"), error_kind="input_boundary")
                    except Exception as exc:
                        kind = _error_kind(exc)
                        row.update(status="error", error_kind=kind)
                        raw = getattr(exc, "raw_content", None)
                        if isinstance(raw, str):
                            row["raw_sha256"] = sha256(raw.encode()).hexdigest()
                        if kind in {"model_version_mismatch", "unapproved_model", "runner_error"}:
                            stop = kind
                    row["elapsed_ms"] = round((monotonic() - before) * 1000, 3)
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                rows.append(row)
    finally:
        await inference.close()
    completed = sum(row["status"] == "ok" for row in rows)
    completion = {"schema_version": 1, "run_id": run_id,
                  "status": "completed" if completed == len(cases) else "incomplete",
                  "planned": len(cases), "completed": completed,
                  "errors": sum(row["status"] == "error" for row in rows),
                  "not_run": sum(row["status"] == "not_run" for row in rows),
                  "input_blocked": sum(row["error_kind"] == "input_boundary" for row in rows),
                  "real_inference_calls": calls, "stop_reason": stop,
                  "elapsed_ms": round((monotonic() - started) * 1000, 3),
                  "predictions_sha256": sha256_file(out / "predictions.jsonl"),
                  "model_quality": "not_run", "production_acceptance": "not_approved"}
    _write_new(out / "completion.json", completion)
    return completion
