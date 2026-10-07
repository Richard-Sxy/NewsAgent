"""
通用业务入口：聊天、写作等通过 run_structured() 调用模型，并绑定租户、追踪编号和调用场景。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from typing import Any, Iterator, Protocol, TypeVar

from pydantic import BaseModel

from app.domain.errors import AgentOutputValidationError
from app.model_runtime.config_file import ModelRuntimeConfig
from app.model_runtime.core import StructuredInferenceService, StructuredOutputError
from app.model_runtime.http import build_chat_completions_payload
from app.model_runtime.result import AgentResult


OutputT = TypeVar("OutputT", bound=BaseModel)
_request_context: ContextVar[tuple[str, str] | None] = ContextVar(
    "newsagent_model_request_context", default=None
)

@contextmanager
def model_request_context(*, tenant_id: str, trace_id: str) -> Iterator[None]:
    if not tenant_id.strip() or not trace_id.strip():
        raise ValueError("model request tenant and trace are required")
    token = _request_context.set((tenant_id, trace_id))
    try:
        yield
    finally:
        _request_context.reset(token)


class StructuredAgentClient(Protocol):
    def input_chars(
        self,
        *,
        mode: str,
        payload: BaseModel | dict[str, Any],
        output_type: type[OutputT],
    ) -> int: ...

    async def run_structured(
        self,
        *,
        app_id: str,
        payload: BaseModel | dict[str, Any],
        output_type: type[OutputT],
        mode: str,
        output_defaults: dict[str, Any] | None = None,
    ) -> AgentResult[OutputT]: ...

"""解析已经批准的场景并调用原始模型推理接口。"""
class NativeStructuredAgentClient:

    def __init__(
        self,
        service: StructuredInferenceService,
        config: ModelRuntimeConfig,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._service = service
        self._config = config
        if timeout_seconds <= 0:
            raise ValueError("model timeout must be positive")
        self._timeout_seconds = timeout_seconds

    def input_chars(
        self,
        *,
        mode: str,
        payload: BaseModel | dict[str, Any],
        output_type: type[OutputT],
    ) -> int:
        """将完整的兼容主体测量为 JSON 字符，而不是模型标记。这种同步操作不需要请求身份、凭证或网络。本地端口使用相同的兼容主体，包括配置的额外字段。
        """
        binding = self._config.agent_scene(mode)
        rendered = self._service.render_input(
            scene=binding.scene, prompt_version=binding.prompt_version,
            payload=payload, output_type=output_type,
        )
        body = build_chat_completions_payload(
            model_route=binding.model_route,
            scene=binding.scene,
            messages=rendered.messages,
            output_json_schema=rendered.output_json_schema,
            response_format=self._config.inference.response_format,
            extra_body=self._config.inference.extra_body,
        )
        return len(json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False))

    async def run_structured(
        self,
        *,
        app_id: str,
        payload: BaseModel | dict[str, Any],
        output_type: type[OutputT],
        mode: str,
        output_defaults: dict[str, Any] | None = None,
    ) -> AgentResult[OutputT]:
        """调用原生 Agent 的结构化推理；不支持外部 App ID。"""
        if not app_id.startswith("python:"):
            raise ValueError("native Agent requires a Python execution identity")
        context = _request_context.get()
        if context is None:
            raise ValueError("native Agent call lacks tenant request context")
        # 根据mode找到对应的Prompt版本和模型路由。
        binding = self._config.agent_scene(mode)
        input_data = (
            payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        )
        encoded = json.dumps(input_data, ensure_ascii=False, sort_keys=True, allow_nan=False)
        identity = sha256(
            f"{context[0]}\x1f{context[1]}\x1f{mode}\x1f{encoded}".encode("utf-8")
        ).hexdigest()
        try:
            # 底层调用 run() 完成结构化推理。
            result = await self._service.run(
                tenant_id=context[0],
                trace_id=context[1],
                idempotency_key=f"agent-{identity}",
                scene=binding.scene,
                prompt_version=binding.prompt_version,
                model_route=binding.model_route,
                payload=input_data,
                output_type=output_type,
                output_defaults=output_defaults,
                timeout_seconds=self._timeout_seconds,
            )
        except StructuredOutputError as exc:
            raise AgentOutputValidationError(
                str(exc), raw_content=exc.raw_content, request_id=exc.request_id
            ) from exc
        # 封装成 AgentResult 返回。
        return AgentResult(
            value=result.value,
            request_id=result.request_id,
            usage=dict(result.usage),
            raw_content=result.raw_content,
        )
