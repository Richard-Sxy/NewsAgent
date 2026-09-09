"""企业统一模型网关的结构化调用契约。"""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.clients.enterprise.common import (
    NonBlank128,
    NonBlank256,
    RpcCallContext,
    RpcResponseMeta,
)


class StructuredModelRequest(BaseModel):
    """热点 Agent 向模型网关提交的版本化、结构化请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scene: Literal["hot_news_analysis"] = "hot_news_analysis"
    model_route: NonBlank128
    prompt_version: NonBlank128
    input_schema_version: NonBlank128
    output_schema_version: NonBlank128
    input_payload: dict[str, JsonValue]
    output_json_schema: dict[str, JsonValue]
    idempotency_key: NonBlank256


class StructuredModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class StructuredModelResponse(BaseModel):
    """模型结果仍是不可信数据，必须继续经过 Pydantic 和业务 Validator。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_payload: dict[str, JsonValue]
    raw_content: str
    model_name: NonBlank128
    model_version: NonBlank128
    finish_reason: NonBlank128 | None = None
    usage: StructuredModelUsage
    meta: RpcResponseMeta


class StructuredModelGatewayRpc(Protocol):
    """企业统一模型服务的 RPC Port，不包含业务重试策略。"""

    async def invoke_structured(
        self,
        *,
        context: RpcCallContext,
        request: StructuredModelRequest,
    ) -> StructuredModelResponse: ...

