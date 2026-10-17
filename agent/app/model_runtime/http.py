"""
实际发送请：调用兼容 Chat Completions、Embeddings 的接口，处理鉴权、超时、错误和响应检查。
"""

from __future__ import annotations

from typing import Any
from time import perf_counter

import httpx
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.model_runtime.config_file import EndpointConfig
from app.model_runtime.core import (
    EmbeddingRequest,
    EmbeddingResult,
    InferenceRequest,
    InferenceMessage,
    RawInferenceResult,
    model_telemetry_attributes,
    provider_response_attributes,
    record_model_call,
    record_model_usage,
)
from app.observability.telemetry import operation, record_error, set_safe_attributes


def build_chat_completions_payload(
    *,
    model_route: str,
    scene: str,
    messages: tuple[InferenceMessage, ...],
    output_json_schema: dict[str, Any],
    response_format: str,
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    """Build the complete inference body for sending and conservative character measurement."""
    payload: dict[str, Any] = {
        "model": model_route,
        "messages": [{"role": message.role, "content": message.content} for message in messages],
        "stream": False,
        "temperature": 0,
    }
    if response_format == "json_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": scene, "strict": True, "schema": output_json_schema},
        }
    elif response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    return {**extra_body, **payload}


class ModelTransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        request_id: str | None = None,
        failure_stage: str = "inference",
        failure_kind: str = "transport_error",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.request_id = request_id
        self.failure_stage = failure_stage
        self.failure_kind = failure_kind


class _HttpModelEndpoint:
    def __init__(
        self,
        config: EndpointConfig,
        *,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config.provider != "openai_compatible" or config.url is None:
            raise ValueError("HTTP model endpoint requires openai_compatible config")
        if not api_key.strip():
            raise ValueError("enterprise model API key cannot be empty")
        self._config = config
        self._api_key = api_key
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(
        self,
        payload: dict[str, Any],
        *,
        tenant_id: str,
        trace_id: str,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        selected = {**(attributes or {}), "http.request.method": "POST", "gen_ai.provider.name": "openai_compatible"}
        with operation("model.http.request", attributes=selected, kind=SpanKind.CLIENT) as span:
            try:
                return await self._post_impl(
                    payload, tenant_id=tenant_id, trace_id=trace_id,
                    idempotency_key=idempotency_key, timeout_seconds=timeout_seconds,
                    span=span,
                )
            except BaseException as exc:
                record_error(span, exc, stage=getattr(exc, "failure_stage", "inference"))
                raise

    async def _post_impl(
        self, payload: dict[str, Any], *, tenant_id: str, trace_id: str,
        idempotency_key: str | None, timeout_seconds: float | None, span: Any,
    ) -> tuple[dict[str, Any], str | None]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Tenant-ID": tenant_id,
            "X-Trace-ID": trace_id,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        # Explicit W3C propagation excludes baggage and identity from trace context.
        TraceContextTextMapPropagator().inject(headers)
        try:
            response = await self._client.post(
                str(self._config.url),
                json={**self._config.extra_body, **payload},
                headers=headers,
                timeout=timeout_seconds or self._config.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelTransportError("enterprise model request timed out", retryable=True, failure_kind="timeout") from exc
        except httpx.RequestError as exc:
            raise ModelTransportError("enterprise model connection failed", retryable=True, failure_kind="connection") from exc

        request_id = response.headers.get("x-request-id")
        set_safe_attributes(span, {
            "http.response.status_code": response.status_code,
            **provider_response_attributes(request_id=request_id),
        })
        if response.status_code >= 400:
            raise ModelTransportError(
                f"enterprise model HTTP {response.status_code}",
                retryable=response.status_code == 429 or response.status_code >= 500,
                request_id=request_id,
                failure_kind="rate_limit" if response.status_code == 429 else "http_error",
            )
        if len(response.content) > 1_048_576:
            raise ModelTransportError(
                "enterprise model response exceeds 1 MiB",
                retryable=False,
                request_id=request_id,
                failure_stage="response_decode", failure_kind="response_too_large",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ModelTransportError(
                "enterprise model response is not JSON",
                retryable=False,
                request_id=request_id,
                failure_stage="response_decode", failure_kind="invalid_json",
            ) from exc
        if not isinstance(body, dict):
            raise ModelTransportError(
                "enterprise model response must be an object",
                retryable=False,
                request_id=request_id,
                failure_stage="response_decode", failure_kind="response_validation",
            )
        return body, request_id


class OpenAICompatibleInferenceClient(_HttpModelEndpoint):
    async def complete(self, request: InferenceRequest) -> RawInferenceResult:
        attributes = model_telemetry_attributes(
            scene=request.scene, model_route=request.model_route,
            prompt_version=request.prompt_version,
        )
        attributes["gen_ai.provider.name"] = "openai_compatible"
        with operation("model.provider.inference", attributes=attributes) as span:
            result = await self._complete(request, attributes=attributes)
            set_safe_attributes(span, provider_response_attributes(
                request_id=result.request_id, model_version=result.model_version,
            ))
            return result

    async def _complete(self, request: InferenceRequest, *, attributes: dict[str, Any]) -> RawInferenceResult:
        if request.model_route not in self._config.model_routes:
            raise ModelTransportError("unapproved inference model route", retryable=False, failure_stage="model_binding", failure_kind="unapproved_model")
        payload = build_chat_completions_payload(
            model_route=request.model_route,
            scene=request.scene,
            messages=request.messages,
            output_json_schema=request.output_json_schema,
            response_format=self._config.response_format,
            extra_body=self._config.extra_body,
        )
        body, request_id = await self._post(
            payload,
            tenant_id=request.tenant_id,
            trace_id=request.trace_id,
            idempotency_key=request.idempotency_key,
            timeout_seconds=request.timeout_seconds,
            attributes=attributes,
        )
        with operation("model.response_validate", attributes=attributes) as span:
            set_safe_attributes(span, provider_response_attributes(request_id=request_id))
            return self._validate_response(request, body=body, request_id=request_id)

    def _validate_response(
        self, request: InferenceRequest, *, body: dict[str, Any], request_id: str | None,
    ) -> RawInferenceResult:
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelTransportError(
                "enterprise inference response lacks message content",
                retryable=False,
                request_id=request_id,
                failure_stage="response_decode", failure_kind="response_validation",
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelTransportError(
                "enterprise inference content must be nonempty text",
                retryable=False,
                request_id=request_id,
                failure_stage="response_decode", failure_kind="response_validation",
            )
        usage = body.get("usage")
        response_model = _optional_string(body.get("model"))
        if response_model != request.model_route:
            raise ModelTransportError(
                "inference model version differs from requested route",
                retryable=False,
                request_id=request_id,
                failure_stage="model_binding", failure_kind="model_version_mismatch",
            )
        return RawInferenceResult(
            content=content,
            request_id=request_id or _optional_string(body.get("id")),
            model_version=response_model,
            usage=usage if isinstance(usage, dict) else {},
        )


class OpenAICompatibleEmbeddingClient(_HttpModelEndpoint):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        attributes = model_telemetry_attributes(
            scene="knowledge_embedding", model_route=request.model_route,
            operation_name="embeddings",
        )
        attributes.update({"gen_ai.provider.name": "openai_compatible", "model.input_count": len(request.texts)})
        started = perf_counter()
        error: BaseException | None = None
        with operation("model.embedding", attributes=attributes) as span:
            try:
                result = await self._embed(request, attributes=attributes, span=span)
                set_safe_attributes(span, {
                    **provider_response_attributes(request_id=result.request_id, model_version=result.model_version),
                    "model.embedding_dimensions": len(result.vectors[0]),
                })
                return result
            except BaseException as exc:
                error = exc
                record_error(span, exc, stage=getattr(exc, "failure_stage", "inference"))
                raise
            finally:
                record_model_call(started=started, attributes=attributes, error=error, stage="inference")

    async def _embed(self, request: EmbeddingRequest, *, attributes: dict[str, Any], span: Any) -> EmbeddingResult:
        if request.model_route not in self._config.model_routes:
            raise ModelTransportError("unapproved embedding model route", retryable=False, failure_stage="model_binding", failure_kind="unapproved_model")
        if len(request.texts) > self._config.max_batch_size:
            raise ModelTransportError("embedding batch exceeds configured limit", retryable=False, failure_stage="model_binding", failure_kind="batch_limit")
        body, request_id = await self._post(
            {"model": request.model_route, "input": list(request.texts)},
            tenant_id=request.tenant_id,
            trace_id=request.trace_id,
            attributes=attributes,
        )
        usage = body.get("usage")
        if isinstance(usage, dict):
            record_model_usage(span, usage, attributes=attributes)
        with operation("model.response_validate", attributes=attributes) as validation_span:
            set_safe_attributes(validation_span, provider_response_attributes(request_id=request_id))
            return self._validate_response(request, body=body, request_id=request_id)

    def _validate_response(
        self, request: EmbeddingRequest, *, body: dict[str, Any], request_id: str | None,
    ) -> EmbeddingResult:
        response_model = _optional_string(body.get("model"))
        if response_model != request.model_route:
            raise ModelTransportError(
                "embedding model version differs from requested route",
                retryable=False,
                request_id=request_id,
                failure_stage="model_binding", failure_kind="model_version_mismatch",
            )
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) != len(request.texts):
            raise ModelTransportError(
                "embedding response count does not match input",
                retryable=False,
                request_id=request_id,
                failure_stage="response_validation", failure_kind="embedding_count",
            )
        vectors_by_index: dict[int, tuple[float, ...]] = {}
        try:
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("embedding row must be an object")
                index = row["index"]
                vector = row["embedding"]
                if type(index) is not int or index in vectors_by_index:
                    raise ValueError("embedding index is invalid or duplicated")
                if not isinstance(vector, list) or any(
                    type(value) not in (int, float) for value in vector
                ):
                    raise ValueError("embedding vector must contain numbers")
                vectors_by_index[index] = tuple(float(value) for value in vector)
            ordered = tuple(vectors_by_index[index] for index in range(len(rows)))
            result = EmbeddingResult(
                vectors=ordered,
                model_version=response_model,
                request_id=request_id or _optional_string(body.get("id")),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ModelTransportError(
                "embedding response failed validation",
                retryable=False,
                request_id=request_id,
                failure_stage="response_validation", failure_kind="embedding_vector",
            ) from exc
        expected = self._config.expected_embedding_dimensions
        if expected is not None and len(result.vectors[0]) != expected:
            raise ModelTransportError(
                "embedding dimension differs from configured index dimension",
                retryable=False,
                request_id=request_id,
                failure_stage="response_validation", failure_kind="embedding_dimension",
            )
        return result


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
