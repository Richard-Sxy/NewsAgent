"""更加配置创建真实 HTTP 客户端和本地模拟实现"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.model_runtime.config_file import (
    EndpointConfig,
    ModelRuntimeConfig,
    validate_runtime_environment,
)
from app.model_runtime.core import (
    EmbeddingPort,
    InferencePort,
    InferenceRequest,
    PromptRegistry,
    RawInferenceResult,
)
from app.model_runtime.http import (
    OpenAICompatibleEmbeddingClient,
    OpenAICompatibleInferenceClient,
)
from app.model_runtime.local_embedding import LocalHashEmbedding
from app.model_runtime.local_inference import LocalHotNewsInference


class _RoutedLocalInference:
    def __init__(self, config: EndpointConfig) -> None:
        self._routes = frozenset(config.model_routes)
        self._inner = LocalHotNewsInference()

    async def complete(self, request: InferenceRequest) -> RawInferenceResult:
        if request.model_route not in self._routes:
            raise ValueError("unapproved local inference model route")
        return await self._inner.complete(request)


@dataclass(slots=True)
class ModelRuntimePorts:
    inference: InferencePort
    embedding: EmbeddingPort
    prompts: PromptRegistry

    async def close(self) -> None:
        for port in (self.inference, self.embedding):
            close = getattr(port, "close", None)
            if close is not None:
                await close()


def build_model_runtime_ports(
    config: ModelRuntimeConfig,
    *,
    environment: str,
) -> ModelRuntimePorts:
    validate_runtime_environment(config, environment=environment)

    def api_key(endpoint: EndpointConfig) -> str:
        variable = endpoint.api_key_env
        value = os.environ.get(variable, "") if variable else ""
        if not value.strip():
            raise ValueError("enterprise model credential environment variable is missing")
        return value

    prompts = config.prompt_registry()
    inference_key = (
        api_key(config.inference)
        if config.inference.provider == "openai_compatible"
        else None
    )
    embedding_key = (
        api_key(config.embedding)
        if config.embedding.provider == "openai_compatible"
        else None
    )

    inference: InferencePort = (
        _RoutedLocalInference(config.inference)
        if config.inference.provider == "local"
        else OpenAICompatibleInferenceClient(
            config.inference, api_key=inference_key or ""
        )
    )
    embedding: EmbeddingPort = (
        LocalHashEmbedding(
            model_routes=config.embedding.model_routes,
            dimensions=config.embedding.local_embedding_dimensions,
        )
        if config.embedding.provider == "local"
        else OpenAICompatibleEmbeddingClient(
            config.embedding, api_key=embedding_key or ""
        )
    )
    return ModelRuntimePorts(
        inference=inference,
        embedding=embedding,
        prompts=prompts,
    )
