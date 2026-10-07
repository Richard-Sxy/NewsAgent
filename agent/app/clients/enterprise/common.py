"""企业 RPC 接入层共用的调用上下文、响应元数据与错误语义。"""

from dataclasses import dataclass
from time import monotonic
from typing import Annotated, Protocol
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints


NonBlank128 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
NonBlank256 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]


class RpcCallContext(BaseModel):
    """由网关或调用方生成的可信 RPC 上下文，不承载访问凭据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: NonBlank128
    request_id: NonBlank128
    trace_id: NonBlank128
    caller_service: NonBlank128
    timeout_ms: int = Field(ge=50, le=120_000)
    idempotency_key: NonBlank256 | None = None


class RpcResponseMeta(BaseModel):
    """企业服务返回的可审计元数据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: NonBlank128
    source_system: NonBlank128
    source_version: NonBlank128
    served_at: AwareDatetime


class RpcCallContextProvider(Protocol):
    """把网关身份与 Trace 上下文转换为一次 RPC 调用上下文。"""

    def create(
        self,
        *,
        tenant_id: str,
        operation: str,
        timeout_ms: int,
        idempotency_key: str | None = None,
    ) -> RpcCallContext: ...


@dataclass(frozen=True, slots=True)
class DefaultRpcCallContextProvider:
    """本地/测试上下文；企业实现应复用网关注入的 Trace。"""

    caller_service: str = "writing-agent-service"

    def create(
        self,
        *,
        tenant_id: str,
        operation: str,
        timeout_ms: int,
        idempotency_key: str | None = None,
    ) -> RpcCallContext:
        operation = operation.strip()
        if not operation:
            raise ValueError("operation cannot be empty")
        trace_id = uuid4().hex
        return RpcCallContext(
            tenant_id=tenant_id,
            request_id=f"{operation}-{uuid4().hex}",
            trace_id=trace_id,
            caller_service=self.caller_service,
            timeout_ms=timeout_ms,
            idempotency_key=idempotency_key,
        )


class EnterpriseRpcError(RuntimeError):
    """所有企业 RPC 错误的基类，供 Temporal 判断是否允许重试。"""

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.error_code = error_code


class EnterpriseRpcTimeoutError(EnterpriseRpcError):
    retryable = True


class EnterpriseRpcUnavailableError(EnterpriseRpcError):
    retryable = True


class EnterpriseRpcRateLimitError(EnterpriseRpcError):
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        error_code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            message,
            request_id=request_id,
            error_code=error_code,
        )
        self.retry_after_seconds = retry_after_seconds


class EnterpriseRpcAuthenticationError(EnterpriseRpcError):
    """调用身份或租户授权不合法，不允许自动重试。"""


class EnterpriseRpcRequestError(EnterpriseRpcError):
    """请求参数、字段映射或业务前置条件不合法。"""


class EnterpriseRpcResponseError(EnterpriseRpcError):
    """RPC 成功返回，但响应无法通过严格契约校验。"""


class RpcDeadlineBudget:
    """为分页 RPC 维持一个总超时预算，而不是每页重新计时。"""

    def __init__(self, context: RpcCallContext) -> None:
        self._context = context
        self._started_at = monotonic()

    def next_context(self) -> RpcCallContext:
        elapsed_ms = int((monotonic() - self._started_at) * 1000)
        remaining_ms = self._context.timeout_ms - elapsed_ms
        if remaining_ms < 50:
            raise EnterpriseRpcTimeoutError(
                "enterprise RPC deadline exhausted",
                request_id=self._context.request_id,
            )
        return self._context.model_copy(
            update={"timeout_ms": remaining_ms}
        )
