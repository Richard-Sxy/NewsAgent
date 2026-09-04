import json
import re
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.domain.errors import (
    AgentOutputValidationError,
    FastGPTAuthenticationError,
    FastGPTNetworkError,
    FastGPTRateLimitError,
    FastGPTRequestError,
    FastGPTResponseError,
    FastGPTServerError,
    FastGPTTimeoutError,
)

OutputT = TypeVar("OutputT", bound=BaseModel)


class FastGPTMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class AgentResult(Generic[OutputT]):
    """保留结构化结果和 AgentRun 审计所需的调用元数据。"""

    value: OutputT
    request_id: str | None
    usage: dict[str, Any]
    raw_content: str


@dataclass(frozen=True)
class FastGPTRawResult:
    content: str
    request_id: str | None
    usage: dict[str, Any]


class FastGPTClient:
    """FastGPT OpenAI-compatible API 的统一异步客户端。"""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 120,
    ) -> None:
        self.base_url = settings.fastgpt_base_url.rstrip("/")
        self.api_key = settings.fastgpt_api_key
        self.timeout = httpx.Timeout(timeout_seconds, connect=10)
        self._owns_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient()

    async def close(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    async def __aenter__(self) -> "FastGPTClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # 执行 App
    async def run_app(
        self,
        *,
        app_id: str,
        messages: list[FastGPTMessage],
        variables: dict[str, Any] | None = None,
    ) -> FastGPTRawResult:
        """执行一个 FastGPT App；重试由 Temporal 统一控制。"""
        try:
            response = await self.http_client.post(
                f"{self.base_url}/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "appId": app_id,
                    "stream": False,
                    "detail": False,
                    "messages": [message.model_dump() for message in messages],
                    "variables": variables or {},
                },
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise FastGPTTimeoutError("FastGPT 请求超时") from exc
        except httpx.RequestError as exc:
            raise FastGPTNetworkError(f"FastGPT 网络错误: {exc}") from exc

        request_id = (
            response.headers.get("x-request-id")
            or response.headers.get("x-fastgpt-request-id")
        )
        self._raise_for_status(response, request_id)

        try:
            body = response.json()
        except ValueError as exc:
            raise FastGPTResponseError(
                "FastGPT 返回的响应不是 JSON",
                request_id=request_id,
            ) from exc

        if not isinstance(body, dict):
            raise FastGPTResponseError(
                "FastGPT 响应必须是 JSON 对象",
                request_id=request_id,
            )
        if body.get("code") not in (None, 200):
            raise FastGPTRequestError(
                str(body.get("message") or "FastGPT 应用执行失败"),
                request_id=request_id,
            )

        payload = body.get("data") if isinstance(body.get("data"), dict) else body
        content = self._extract_content(payload, request_id)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return FastGPTRawResult(
            content=content,
            request_id=request_id,
            usage=usage,
        )

    async def run_structured(
        self,
        *,
        app_id: str,
        payload: BaseModel | dict[str, Any],
        output_type: type[OutputT],
        mode: str,
        output_defaults: dict[str, Any] | None = None,
    ) -> AgentResult[OutputT]:
        """执行 App，并将返回内容严格校验为指定 Pydantic 类型。"""
        input_data = (
            payload.model_dump(mode="json")
            if isinstance(payload, BaseModel)
            else payload
        )
        raw = await self.run_app(
            app_id=app_id,
            messages=[
                FastGPTMessage(
                    role="user",
                    content=json.dumps(input_data, ensure_ascii=False),
                )
            ],
            variables={
                "mode": mode,
                "output_schema": output_type.model_json_schema(),
            },
        )
        json_content = self._unwrap_json_code_fence(raw.content)
        try:
            decoded = json.loads(json_content)
            if output_defaults and isinstance(decoded, dict):
                for key, default in output_defaults.items():
                    if decoded.get(key) in (None, ""):
                        decoded[key] = default
            value = output_type.model_validate(decoded)
        except (ValidationError, ValueError) as exc:
            raise AgentOutputValidationError(
                f"Agent 输出不符合 {output_type.__name__}: {exc}",
                raw_content=raw.content,
                request_id=raw.request_id,
            ) from exc
        return AgentResult(
            value=value,
            request_id=raw.request_id,
            usage=raw.usage,
            raw_content=raw.content,
        )

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        request_id: str | None,
    ) -> None:
        status = response.status_code
        detail = response.text[:2000]
        if status in (401, 403):
            raise FastGPTAuthenticationError(detail, request_id=request_id)
        if status == 429:
            retry_after: float | None = None
            try:
                retry_after = float(response.headers["retry-after"])
            except (KeyError, ValueError):
                pass
            raise FastGPTRateLimitError(
                detail,
                request_id=request_id,
                retry_after=retry_after,
            )
        if status >= 500:
            raise FastGPTServerError(detail, request_id=request_id)
        if status >= 400:
            raise FastGPTRequestError(detail, request_id=request_id)

    @staticmethod
    def _extract_content(payload: dict[str, Any], request_id: str | None) -> str:
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise FastGPTResponseError(
                "FastGPT 响应缺少 choices[0].message.content",
                request_id=request_id,
            ) from exc
        if isinstance(content, list):
            content = "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise FastGPTResponseError(
                "FastGPT 返回了空内容",
                request_id=request_id,
            )
        return content.strip()

    @staticmethod
    def _unwrap_json_code_fence(content: str) -> str:
        """只允许纯 JSON 或完整 JSON 代码块，拒绝夹杂解释文本。"""
        stripped = content.strip()
        match = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            stripped,
            flags=re.DOTALL | re.IGNORECASE,
        )
        return match.group(1).strip() if match else stripped
