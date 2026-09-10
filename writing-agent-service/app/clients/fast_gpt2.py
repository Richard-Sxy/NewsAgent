import httpx
from app.config import Settings
from dataclasses import dataclass
from typing import Any, Literal
from pydantic import BaseModel

from app.domain.error import (
    AgentOutputValidationError,
    FastGPTResponseError,
    FastGPTTimeoutError,
    FastGPTError,
    FastGPTAuthenticationError,
    FastGPTNetworkError,
    FastGPTRateLimitError
)

class FastGPTMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

@dataclass
class FastGPTRawResult:
    content: str
    request_id: str | None
    usage: dict[str, Any]

class FastGPTClient:
    """统一的异步客户端。"""
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 120,
    ) -> None:
        self.base_url = Settings.fastgpt_base_url.rstrip("/")
        self.api_key = Settings.fastgpt_api_key
        self.timeout = httpx.Timeout(timeout_seconds, connect=10)
        self._own_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient()

    async def close(self) -> None:
        if self._own_client:
            await self.http_client.aclose()

    async def __aenter__(self) -> "FastGPTClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def run_app(
        self,
        *,
        app_id: str,
        messages: list[FastGPTMessage],
        variables: dict[str, Any] | None = None
    ) -> FastGPTRawResult:
        """执行一个"""
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
            raise FastGPTTimeoutError("FastGPT请求超时") from exc
        except httpx.RequestError as exc:
            raise FastGPTNetworkError(f"FastGPT网络错误：{exc}") from exc

        request_id = (
            response.headers.get("x-request-id")
            or response.headers.get("x-fastgpt-request-id")
        )
        self._raise_for_status(response, request_id)