"""Generation-only SQL proposals using a fixed DB-GPT-derived prompt asset.

This module owns no warehouse, connector, execution callback or DB-GPT runtime.
The caller must validate the proposal against the native compiler and SQL guard
before selecting it. One model attempt shares one deadline; no repair/fallback.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
from time import monotonic
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from app.model_runtime.agent_client import NativeStructuredAgentClient, model_request_context
from app.model_runtime.config_file import AgentSceneConfig, ModelRuntimeConfig, PromptConfig
from app.model_runtime.core import InferencePort, StructuredInferenceService
from app.schemas.sql_assistant import SqlAssistantIntent
from app.sql_assistant.candidate_validation import validator_identity
from app.sql_assistant.planner import validate_intent
from app.sql_assistant.scenarios import SqlScenario


ASSETS = Path(__file__).with_name("assets")
UPSTREAM_COMMIT = "83609c471266607490b24e72188311efa23a0d16"
PROMPT_VERSION = "dbgpt-template-candidate-v1"
_response_metadata: ContextVar[dict | None] = ContextVar("sql_candidate_response_metadata", default=None)


class SqlCandidateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1
    mode: Literal["native", "dbgpt_shadow", "dbgpt_candidate"] = "native"
    deadline_seconds: float = Field(default=10, gt=0, le=30)
    max_output_tokens: int = Field(default=2048, ge=128, le=8192, strict=True)
    max_input_chars: int = Field(default=40000, ge=1000, le=200000, strict=True)
    max_output_chars: int = Field(default=16000, ge=256, le=64000, strict=True)
    model_scene: str = Field(default="text2sql_assistant", min_length=1, max_length=128)
    max_model_attempts: Literal[1] = 1
    max_repair_attempts: Literal[0] = 0
    allow_native_fallback: Literal[False] = False


def load_sql_candidate_config(path: str | None = None) -> SqlCandidateConfig:
    if not path:
        return SqlCandidateConfig()
    source = Path(path)
    if not source.is_file() or source.stat().st_size > 65536:
        raise ValueError("SQL candidate config missing or exceeds 64 KiB")
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
        return SqlCandidateConfig.model_validate(data)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise ValueError("SQL candidate configuration is invalid") from exc


class SqlCandidateResponse(BaseModel):
    """Model-owned fields only; generation metadata is never model input/output."""
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    status: Literal["proposed", "unsupported"]
    sql: str | None
    reason_code: Literal["unsupported_contract"] | None

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "proposed":
            if self.sql is None or not self.sql.strip() or self.reason_code is not None:
                raise ValueError("proposed requires non-empty SQL and no reason")
        elif self.sql is not None or self.reason_code != "unsupported_contract":
            raise ValueError("unsupported requires null SQL and unsupported_contract")
        return self


@dataclass(frozen=True)
class SqlCandidateResult:
    status: Literal["proposed", "unsupported"]
    sql: str | None
    reason_code: str | None
    metadata: dict[str, Any]

    @property
    def generation_metadata(self) -> dict[str, Any]:
        return self.metadata


class SqlCandidateError(ValueError):
    retryable = False

    def __init__(self, code: str, *, metadata: dict[str, Any] | None = None):
        super().__init__("SQL candidate generation failed: " + code)
        self.code = code
        self.metadata = metadata or {}
        self.failure_kind = code
        self.failure_stage = "sql_candidate"


class SqlCandidateGenerator(Protocol):
    def identity(self) -> dict[str, Any]: ...

    async def generate(
        self, *, intent: SqlAssistantIntent, scenario: SqlScenario,
        schema_ddl: str, schema_version: str, tenant_id: str, request_id: str,
    ) -> SqlCandidateResult: ...


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _read_asset(name: str) -> tuple[str, str]:
    data = (ASSETS / name).read_bytes()
    if len(data) > 65536:
        raise ValueError("SQL candidate asset exceeds 64 KiB")
    return data.decode("utf-8-sig"), sha256(data).hexdigest()


def _safe_identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,191}", value) else None


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        value = dict(value)
    return {key: count for key, count in value.items()
            if key in {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens"}
            and type(count) is int and count >= 0}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON constant")


def parse_candidate_response(content: str, *, max_output_chars: int) -> SqlCandidateResponse:
    """No fences, free text, duplicate fields or extracted SQL fragments."""
    if not isinstance(content, str) or len(content) > max_output_chars:
        raise SqlCandidateError("response_too_large")
    try:
        decoded = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        raise SqlCandidateError("invalid_json") from None
    try:
        return SqlCandidateResponse.model_validate(decoded)
    except ValueError:
        raise SqlCandidateError("invalid_response") from None


class _StrictCandidateInference:
    """Enforce output protocol before the general structured parser sees it."""
    def __init__(self, inference: InferencePort, config: SqlCandidateConfig):
        self.inference, self.config = inference, config

    async def complete(self, request):
        raw = await self.inference.complete(replace(request, max_output_tokens=self.config.max_output_tokens))
        _response_metadata.set({"model_version": _safe_identifier(raw.model_version)})
        try:
            parse_candidate_response(raw.content, max_output_chars=self.config.max_output_chars)
        except SqlCandidateError as exc:
            exc.metadata = {"model_request_id": _safe_identifier(raw.request_id),
                            "model_version": _safe_identifier(raw.model_version), "usage": _usage(raw.usage)}
            raise
        return raw


def candidate_model_config(model_config: ModelRuntimeConfig, config: SqlCandidateConfig) -> ModelRuntimeConfig:
    """Create a prompt/scene-only view; retain the existing endpoint and credential injection."""
    binding = model_config.agent_scene(config.model_scene)
    prompt, _ = _read_asset("candidate_prompt_v1.txt")
    scene = "sql_candidate_shadow" if config.mode == "dbgpt_shadow" else "sql_candidate"
    # The per-request token cap is sent by the wrapper. This copied extra_body
    # only makes NativeStructuredAgentClient.input_chars measure the same field.
    endpoint = model_config.inference.model_dump()
    token_key = "max_completion_tokens" if "max_completion_tokens" in endpoint["extra_body"] else "max_tokens"
    extra = {key: value for key, value in endpoint["extra_body"].items()
             if key not in {"max_tokens", "max_completion_tokens"}}
    extra[token_key] = config.max_output_tokens
    endpoint["extra_body"] = extra
    endpoint["timeout_seconds"] = min(endpoint["timeout_seconds"], config.deadline_seconds)
    data = model_config.model_dump()
    data["inference"] = endpoint
    data["prompts"] = [PromptConfig(scene=scene, version=PROMPT_VERSION, system_prompt=prompt).model_dump()]
    data["agent_scenes"] = [AgentSceneConfig(scene=scene, prompt_version=PROMPT_VERSION,
                                            model_route=binding.model_route).model_dump()]
    return ModelRuntimeConfig.model_validate(data)


class DbgptTemplateCandidateGenerator:
    def __init__(self, *, config: SqlCandidateConfig, model_config: ModelRuntimeConfig,
                 inference: InferencePort):
        if config.mode == "native":
            raise ValueError("native mode does not create a candidate generator")
        self.config = config
        runtime = candidate_model_config(model_config, config)
        binding = runtime.agent_scenes[0]
        self._scene = binding.scene
        self._client = NativeStructuredAgentClient(
            StructuredInferenceService(inference=_StrictCandidateInference(inference, config),
                                       prompts=runtime.prompt_registry(), max_output_chars=config.max_output_chars),
            runtime, timeout_seconds=min(config.deadline_seconds, runtime.inference.timeout_seconds),
        )
        catalog, catalog_hash = _read_asset("candidate_catalog_v1.json")
        examples, examples_hash = _read_asset("candidate_examples_v1.json")
        source, source_hash = _read_asset("dbgpt_source.json")
        provenance = json.loads(source)
        if provenance["commit"] != UPSTREAM_COMMIT:
            raise ValueError("candidate upstream identity mismatch")
        self._catalog = json.loads(catalog)
        self._examples = json.loads(examples)
        if self._examples["examples"]:
            raise ValueError("candidate examples require a future reviewed scope-selection contract")
        _, prompt_hash = _read_asset("candidate_prompt_v1.txt")
        source_code_hash = sha256(Path(__file__).read_bytes()).hexdigest()
        validator = validator_identity()
        self._identity = {
            "provider": "dbgpt_prompt_asset", "reuse_scope": "prompt_template_only",
            "upstream_commit": UPSTREAM_COMMIT, "source_manifest_sha256": source_hash,
            "adapter_version": "sql-candidate-v1", "adapter_sha256": source_code_hash,
            "validator": validator, "validator_sha256": validator["source_sha256"],
            "response_schema_sha256": _digest(SqlCandidateResponse.model_json_schema()),
            "prompt_version": PROMPT_VERSION, "prompt_sha256": prompt_hash,
            "catalog_version": self._catalog["version"], "catalog_sha256": catalog_hash,
            "examples_version": self._examples["version"], "examples_sha256": examples_hash,
            "model_route": binding.model_route, "model_provider": runtime.inference.provider,
            "scene": binding.scene, "config": config.model_dump(mode="json"),
        }

    def identity(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._identity))

    def _payload(self, intent, scenario, schema_ddl, schema_version):
        dimensions = self._catalog["ranking_dimensions" if scenario.result_mode == "ranking" else "trend_dimensions"]
        predicates = list(self._catalog["base_predicates"])
        parameters = ["tenant_id", "window_start", "window_end", "row_limit"]
        for field in ("content_type", "category"):
            if getattr(intent, field) is not None:
                predicates.append(f"{field} = :{field}")
                parameters.append(field)
        order = [f"{intent.sort_by} {intent.sort_direction.upper()}", "news_id ASC"] if scenario.result_mode == "ranking" else ["event_time ASC"]
        return {
            "contract_version": "sql-candidate-v1", "schema_version": schema_version,
            "schema_ddl": schema_ddl, "confirmed_intent": intent.model_dump(exclude={"explanation"}),
            "result_mode": scenario.result_mode, "catalog": self._catalog,
            "required_projection": [*dimensions, *self._catalog["measures"]],
            "required_predicates": predicates, "group_by": dimensions, "order_by": order,
            "allowed_parameters": parameters, "examples": self._examples["examples"],
        }

    async def generate(self, *, intent: SqlAssistantIntent, scenario: SqlScenario,
                       schema_ddl: str, schema_version: str, tenant_id: str, request_id: str) -> SqlCandidateResult:
        started = monotonic()
        metadata = {**self.identity(), "request_id": request_id, "attempts": 0}
        response_token = _response_metadata.set(None)
        try:
            intent = SqlAssistantIntent.model_validate(intent.model_dump())
            scenario = SqlScenario.model_validate(scenario.model_dump())
            validate_intent(intent, scenario)
            if not schema_ddl.strip() or not schema_version.strip():
                raise SqlCandidateError("invalid_contract")
            if len(schema_ddl) > self.config.max_input_chars:
                raise SqlCandidateError("input_too_large")
            payload = self._payload(intent, scenario, schema_ddl, schema_version)
            metadata.update({"schema_version": schema_version, "schema_sha256": sha256(schema_ddl.encode()).hexdigest(),
                             "intent_sha256": _digest(intent.model_dump(exclude={"explanation"})),
                             "scenario_sha256": _digest(scenario.model_dump(mode="json"))})
            if self._client.input_chars(mode=self._scene, payload=payload, output_type=SqlCandidateResponse) > self.config.max_input_chars:
                raise SqlCandidateError("input_too_large")
            remaining = self.config.deadline_seconds - (monotonic() - started)
            if remaining <= 0:
                raise SqlCandidateError("deadline_exceeded")
            with model_request_context(tenant_id=tenant_id, trace_id=f"sql-candidate-{request_id}"):
                metadata["attempts"] = 1
                async with asyncio.timeout(remaining):
                    result = await self._client.run_structured(app_id="python:sql-candidate-v1", payload=payload,
                                                               output_type=SqlCandidateResponse, mode=self._scene)
            if monotonic() - started > self.config.deadline_seconds:
                raise SqlCandidateError("deadline_exceeded")
            metadata.update(_response_metadata.get() or {})
            metadata.update({"model_request_id": _safe_identifier(result.request_id), "usage": _usage(result.usage),
                             "elapsed_ms": int((monotonic() - started) * 1000)})
            return SqlCandidateResult(result.value.status, result.value.sql, result.value.reason_code, metadata)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            kind = getattr(exc, "failure_kind", "model_failure")
            permitted = {"timeout", "model_deadline", "rate_limit", "quota_wait_timeout",
                         "quota_policy_mismatch", "quota_backend_unavailable", "connection", "http_error"}
            code = exc.code if isinstance(exc, SqlCandidateError) else "deadline_exceeded" if isinstance(exc, TimeoutError) else kind if kind in permitted else "model_failure"
            metadata.update(getattr(exc, "metadata", {}) if isinstance(exc, SqlCandidateError) else {})
            metadata["elapsed_ms"] = int((monotonic() - started) * 1000)
            raise SqlCandidateError(code, metadata=metadata) from None
        finally:
            _response_metadata.reset(response_token)


def build_sql_candidate_generator(*, config: SqlCandidateConfig, model_config: ModelRuntimeConfig,
                                  inference: InferencePort) -> DbgptTemplateCandidateGenerator | None:
    if config.mode == "native":
        return None
    return DbgptTemplateCandidateGenerator(config=config, model_config=model_config, inference=inference)
