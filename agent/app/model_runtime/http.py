"""
实际发送请：调用兼容 Chat Completions、Embeddings 的接口，处理鉴权、超时、错误和响应检查。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.model_runtime.config_file import EndpointConfig
from app.model_runtime.core import (
    EmbeddingRequest,
    EmbeddingResult,
    InferenceRequest,
    InferenceMessage,
    RawInferenceResult,
)


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
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.request_id = request_id


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
    ) -> tuple[dict[str, Any], str | None]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Tenant-ID": tenant_id,
            "X-Trace-ID": trace_id,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self._client.post(
                str(self._config.url),
                json={**self._config.extra_body, **payload},
                headers=headers,
                timeout=timeout_seconds or self._config.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelTransportError("enterprise model request timed out", retryable=True) from exc
        except httpx.RequestError as exc:
            raise ModelTransportError("enterprise model connection failed", retryable=True) from exc

        request_id = response.headers.get("x-request-id")
        if response.status_code >= 400:
            raise ModelTransportError(
                f"enterprise model HTTP {response.status_code}",
                retryable=response.status_code == 429 or response.status_code >= 500,
                request_id=request_id,
            )
        if len(response.content) > 1_048_576:
            raise ModelTransportError(
                "enterprise model response exceeds 1 MiB",
                retryable=False,
                request_id=request_id,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ModelTransportError(
                "enterprise model response is not JSON",
                retryable=False,
                request_id=request_id,
            ) from exc
        if not isinstance(body, dict):
            raise ModelTransportError(
                "enterprise model response must be an object",
                retryable=False,
                request_id=request_id,
            )
        return body, request_id


class OpenAICompatibleInferenceClient(_HttpModelEndpoint):
    async def complete(self, request: InferenceRequest) -> RawInferenceResult:
        if request.model_route not in self._config.model_routes:
            raise ModelTransportError("unapproved inference model route", retryable=False)
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
        )
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelTransportError(
                "enterprise inference response lacks message content",
                retryable=False,
                request_id=request_id,
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelTransportError(
                "enterprise inference content must be nonempty text",
                retryable=False,
                request_id=request_id,
            )
        usage = body.get("usage")
        response_model = _optional_string(body.get("model"))
        if response_model != request.model_route:
            raise ModelTransportError(
                "inference model version differs from requested route",
                retryable=False,
                request_id=request_id,
            )
        return RawInferenceResult(
            content=content,
            request_id=request_id or _optional_string(body.get("id")),
            model_version=response_model,
            usage=usage if isinstance(usage, dict) else {},
        )


class OpenAICompatibleEmbeddingClient(_HttpModelEndpoint):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        if request.model_route not in self._config.model_routes:
            raise ModelTransportError("unapproved embedding model route", retryable=False)
        if len(request.texts) > self._config.max_batch_size:
            raise ModelTransportError("embedding batch exceeds configured limit", retryable=False)
        body, request_id = await self._post(
            {"model": request.model_route, "input": list(request.texts)},
            tenant_id=request.tenant_id,
            trace_id=request.trace_id,
        )
        response_model = _optional_string(body.get("model"))
        if response_model != request.model_route:
            raise ModelTransportError(
                "embedding model version differs from requested route",
                retryable=False,
                request_id=request_id,
            )
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) != len(request.texts):
            raise ModelTransportError(
                "embedding response count does not match input",
                retryable=False,
                request_id=request_id,
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
            ) from exc
        expected = self._config.expected_embedding_dimensions
        if expected is not None and len(result.vectors[0]) != expected:
            raise ModelTransportError(
                "embedding dimension differs from configured index dimension",
                retryable=False,
                request_id=request_id,
            )
        return result


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
