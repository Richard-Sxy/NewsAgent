"""混元视频理解接入：企业模型网关 Adapter 与混元协议参考实现。

分两层，与 ``app/clients/enterprise/README.md`` 的防腐层约定一致：

```text
VideoContentTextualizer（领域）
→ VideoUnderstandingModel（领域 Port）
→ RpcVideoUnderstandingModel（Adapter：领域语义 ↔ 模型网关契约）
→ MultimodalModelGatewayRpc（企业 Port）
   ├─ 企业统一模型网关（生产：由企业路由到混元）
   └─ HunyuanVisionVideoRpc（参考实现：直连混元 OpenAI 兼容接口）
```

两点必须说清楚：

1. 生产环境应优先使用企业统一模型网关（``RpcVideoUnderstandingModel`` 包装它），
   这样鉴权、限流、路由、审计都由企业侧统一管理。
2. ``HunyuanVisionVideoRpc`` 是协议参考实现，用于网关尚未就绪时的本地联调，
   或用于验证混元的视频请求体形态。它不包含企业鉴权体系。

混元视频协议要点（据腾讯云官方文档）：

- OpenAI 兼容接口 ``POST {base_url}/chat/completions``，模型形如
  ``hunyuan-turbos-vision-video-20250728``。
- 消息内容块为 ``{"type": "video_url", "video_url": {"url": ...}}`` 加一个
  ``{"type": "text", "text": ...}``。
- 腾讯云 API 3.0 版本在 ``VideoUrl.Fps`` 显式支持抽帧率；OpenAI 兼容层是否透传
  ``fps`` 取决于网关实现，因此本 Adapter 提供 ``send_fps`` 开关。
- 混元不支持 Base64 传视频，必须使用 URL。企业内网视频地址需要网关侧可达，
  否则应先由企业侧转存/代理，本项目不下载视频本体。
"""

from datetime import datetime, timezone
from typing import Any, Mapping

import httpx
from pydantic import ValidationError

from app.analytics.video_textualization import (
    VideoSummary,
    VideoSummaryRequest,
    VideoSummaryUnavailableError,
    VideoUnderstandingModel,
)
from app.clients.enterprise.common import (
    DefaultRpcCallContextProvider,
    EnterpriseRpcAuthenticationError,
    EnterpriseRpcError,
    EnterpriseRpcRateLimitError,
    EnterpriseRpcResponseError,
    EnterpriseRpcTimeoutError,
    EnterpriseRpcUnavailableError,
    RpcCallContext,
    RpcCallContextProvider,
    RpcResponseMeta,
)
from app.clients.enterprise.multimodal_gateway import (
    MultimodalModelGatewayRpc,
    VideoUnderstandingCallRequest,
    VideoUnderstandingCallResponse,
    VideoUnderstandingCallUsage,
)


class HunyuanVisionVideoRpc:
    """混元视频理解协议的参考实现，可直接作为 ``MultimodalModelGatewayRpc`` 使用。

    仅用于联调与协议验证；生产环境请替换为企业统一模型网关。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 120,
        send_fps: bool = True,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url cannot be empty")
        if not api_key.strip():
            raise ValueError("api_key cannot be empty")
        if not model.strip():
            raise ValueError("model cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.http_client = http_client or httpx.AsyncClient()
        self._owns_client = http_client is None
        self.timeout = httpx.Timeout(timeout_seconds, connect=10)
        self.send_fps = send_fps

    async def close(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    @staticmethod
    def build_payload(
        request: VideoUnderstandingCallRequest,
        *,
        model: str,
        send_fps: bool = True,
    ) -> dict[str, Any]:
        """构造混元 OpenAI 兼容请求体，供联调与单测复用。"""

        video_block: dict[str, Any] = {"url": request.video_url}
        if send_fps:
            video_block["fps"] = request.fps
        return {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "video_url", "video_url": video_block},
                        {"type": "text", "text": request.prompt},
                    ],
                }
            ],
            "stream": False,
        }

    async def invoke_video_understanding(
        self,
        *,
        context: RpcCallContext,
        request: VideoUnderstandingCallRequest,
    ) -> VideoUnderstandingCallResponse:
        try:
            response = await self.http_client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "X-Request-Id": context.request_id,
                },
                json=self.build_payload(
                    request,
                    model=self.model,
                    send_fps=self.send_fps,
                ),
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise EnterpriseRpcTimeoutError(
                "hunyuan video understanding timed out",
                request_id=context.request_id,
            ) from exc
        except httpx.HTTPError as exc:
            raise EnterpriseRpcUnavailableError(
                f"hunyuan video understanding transport error: {exc}",
                request_id=context.request_id,
            ) from exc

        self._raise_for_status(response, request_id=context.request_id)
        return self._build_response(response, request_id=context.request_id)

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        *,
        request_id: str,
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        detail = response.text.strip()[:500]
        if status in (401, 403):
            raise EnterpriseRpcAuthenticationError(
                f"hunyuan video understanding unauthorized: {detail}",
                request_id=request_id,
                error_code=str(status),
            )
        if status == 429:
            raise EnterpriseRpcRateLimitError(
                f"hunyuan video understanding rate limited: {detail}",
                request_id=request_id,
                error_code=str(status),
            )
        if status >= 500:
            raise EnterpriseRpcUnavailableError(
                f"hunyuan video understanding unavailable: {detail}",
                request_id=request_id,
                error_code=str(status),
            )
        raise EnterpriseRpcResponseError(
            f"hunyuan video understanding rejected request: {detail}",
            request_id=request_id,
            error_code=str(status),
        )

    @staticmethod
    def _extract_text(message: Mapping[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n".join(parts).strip()
        return ""

    @classmethod
    def _build_response(
        cls,
        response: httpx.Response,
        *,
        request_id: str,
    ) -> VideoUnderstandingCallResponse:
        try:
            body = response.json()
        except ValueError as exc:
            raise EnterpriseRpcResponseError(
                "hunyuan video understanding returned non-JSON body",
                request_id=request_id,
            ) from exc
        if not isinstance(body, dict):
            raise EnterpriseRpcResponseError(
                "hunyuan video understanding returned unexpected payload",
                request_id=request_id,
            )

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise EnterpriseRpcResponseError(
                "hunyuan video understanding response is missing choices",
                request_id=request_id,
            )
        first_choice = choices[0]
        message = first_choice.get("message") if isinstance(first_choice, dict) else None
        if not isinstance(message, dict):
            raise EnterpriseRpcResponseError(
                "hunyuan video understanding response is missing message",
                request_id=request_id,
            )

        usage = body.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        model_name = str(body.get("model") or "").strip() or "unknown"
        try:
            return VideoUnderstandingCallResponse(
                summary=cls._extract_text(message),
                model_name=model_name,
                model_version=model_name,
                finish_reason=(
                    str(first_choice.get("finish_reason") or "").strip() or None
                ),
                usage=VideoUnderstandingCallUsage(
                    prompt_tokens=_non_negative_int(usage.get("prompt_tokens")),
                    completion_tokens=_non_negative_int(
                        usage.get("completion_tokens")
                    ),
                    total_tokens=_non_negative_int(usage.get("total_tokens")),
                ),
                meta=RpcResponseMeta(
                    request_id=str(body.get("id") or "").strip() or request_id,
                    source_system="hunyuan-vision-video",
                    source_version=model_name,
                    served_at=datetime.now(tz=timezone.utc),
                ),
            )
        except ValidationError as exc:
            raise EnterpriseRpcResponseError(
                f"hunyuan video understanding response failed contract: {exc}",
                request_id=request_id,
            ) from exc


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


class RpcVideoUnderstandingModel(VideoUnderstandingModel):
    """把领域请求翻译为企业模型网关调用，并做严格的响应校验。

    职责边界：

    - 生成带租户与 Trace 的可信调用上下文，并派生幂等键。
    - 把 ``EnterpriseRpcError`` 一律翻译为领域层的
      ``VideoSummaryUnavailableError``，让上层走降级链而不是整体失败；
      同时保留原始 ``retryable`` 语义供 Temporal 判断。
    - 校验摘要非空，拒绝空白或超长输出。
    """

    def __init__(
        self,
        client: MultimodalModelGatewayRpc,
        *,
        model_route: str,
        context_provider: RpcCallContextProvider | None = None,
        timeout_ms: int = 120_000,
        max_summary_chars: int = 4_000,
    ) -> None:
        if not model_route.strip():
            raise ValueError("model_route cannot be empty")
        if not 50 <= timeout_ms <= 120_000:
            raise ValueError("timeout_ms must be between 50 and 120000")
        if max_summary_chars <= 0:
            raise ValueError("max_summary_chars must be greater than 0")
        self._client = client
        self._model_route = model_route
        self._context_provider = (
            context_provider or DefaultRpcCallContextProvider()
        )
        self._timeout_ms = timeout_ms
        self._max_summary_chars = max_summary_chars

    async def summarize_video(
        self,
        *,
        tenant_id: str,
        request: VideoSummaryRequest,
    ) -> VideoSummary:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        context = self._context_provider.create(
            tenant_id=tenant_id,
            operation="video-news-summarization",
            timeout_ms=self._timeout_ms,
            idempotency_key=self._idempotency_key(request),
        )

        try:
            response = await self._client.invoke_video_understanding(
                context=context,
                request=VideoUnderstandingCallRequest(
                    model_route=self._model_route,
                    video_url=request.video_url,
                    prompt=request.prompt,
                    prompt_version=request.prompt_version,
                    fps=request.fps,
                    max_output_chars=request.max_output_chars,
                    idempotency_key=self._idempotency_key(request),
                ),
            )
        except EnterpriseRpcError as exc:
            raise VideoSummaryUnavailableError(
                f"{type(exc).__name__}: {exc}",
                retryable=exc.retryable,
            ) from exc

        summary = _coerce_summary(response, max_chars=self._max_summary_chars)
        if not summary.strip():
            raise VideoSummaryUnavailableError(
                "model gateway returned an empty video summary",
                retryable=False,
            )
        model_name, model_version, total_tokens = _coerce_evidence(response)
        return VideoSummary(
            text=summary,
            model_name=model_name,
            model_version=model_version,
            total_tokens=total_tokens,
        )

    @staticmethod
    def _idempotency_key(request: VideoSummaryRequest) -> str:
        """同一视频 + 同一策略 + 同一 Prompt 版本可安全复用同一次概括。"""

        raw = (
            f"{request.news_id}|{request.prompt_version}|"
            f"{request.fps}|{request.max_output_chars}"
        )
        return raw[:256]


def _coerce_summary(response: Any, *, max_chars: int) -> str:
    """兼容 Pydantic 契约对象与映射两种返回形态。"""

    value = getattr(response, "summary", None)
    if value is None and isinstance(response, Mapping):
        value = response.get("summary")
    text = str(value or "").strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def _coerce_evidence(response: Any) -> tuple[str, str, int]:
    def _get(name: str, default: Any = None) -> Any:
        value = getattr(response, name, None)
        if value is None and isinstance(response, Mapping):
            value = response.get(name)
        return default if value is None else value

    model_name = str(_get("model_name", "unknown") or "unknown")
    model_version = str(_get("model_version", model_name) or model_name)
    usage = _get("usage")
    total_tokens = 0
    if isinstance(usage, Mapping):
        raw_total = usage.get("total_tokens")
        if isinstance(raw_total, int):
            total_tokens = raw_total
    elif isinstance(usage, VideoUnderstandingCallUsage):
        total_tokens = usage.total_tokens
    return model_name, model_version, total_tokens
