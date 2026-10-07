"""混元视频理解协议与领域 Adapter 的传输契约测试。

用 ``httpx.MockTransport`` 模拟混元 OpenAI 兼容接口，不访问真实网络，
用于锁定三件事：

1. 请求体形态：``video_url`` + ``fps`` + ``prompt`` 确实按混元协议发出。
2. 响应解析：能取出 summary / model / usage，并带上溯源元数据。
3. 错误翻译：``EnterpriseRpcError`` 被翻译为可降级的
   ``VideoSummaryUnavailableError``，且 ``retryable`` 语义不丢失。
"""

import json
from typing import Any, Callable

import httpx
import pytest

from app.analytics.video_textualization import (
    VideoSummaryRequest,
    VideoSummaryUnavailableError,
)
from app.clients.enterprise.common import (
    DefaultRpcCallContextProvider,
    EnterpriseRpcResponseError,
)
from app.clients.enterprise.hunyuan_video import (
    HunyuanVisionVideoRpc,
    RpcVideoUnderstandingModel,
)
from app.clients.enterprise.multimodal_gateway import VideoUnderstandingCallRequest

MODEL = "hunyuan-turbos-vision-video-20250728"
VIDEO_URL = "https://video.example.com/20260910V00001.mp4"

SUCCESS_BODY: dict[str, Any] = {
    "id": "chatcmpl-1",
    "model": MODEL,
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": "视频显示沿海城市出现强风暴雨，气象部门称台风已于当晚登陆。",
            },
        }
    ],
    "usage": {"prompt_tokens": 823, "completion_tokens": 23, "total_tokens": 846},
}


def make_request(**overrides) -> VideoSummaryRequest:
    params = {
        "news_id": "20260910V00001",
        "title": "台风登陆沿海地区",
        "video_url": VIDEO_URL,
        "prompt": "请概括这段新闻视频。",
        "prompt_version": "video-summary-v1",
        "fps": 1.0,
        "max_output_chars": 600,
    }
    params.update(overrides)
    return VideoSummaryRequest(**params)


def make_model(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    send_fps: bool = True,
) -> RpcVideoUnderstandingModel:
    client = HunyuanVisionVideoRpc(
        base_url="https://tokenhub.tencentmaas.test/v1",
        api_key="test-key",
        model=MODEL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        send_fps=send_fps,
    )
    return RpcVideoUnderstandingModel(client, model_route=MODEL)


@pytest.mark.asyncio
async def test_hunyuan_payload_follows_video_url_protocol() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=SUCCESS_BODY)

    model = make_model(handler)
    result = await model.summarize_video(tenant_id="tenant-1", request=make_request())

    assert result.text.startswith("视频显示沿海城市")
    assert result.model_name == MODEL
    assert result.total_tokens == 846

    payload = captured[0]
    assert payload["model"] == MODEL
    assert payload["stream"] is False
    content = payload["messages"][0]["content"]
    assert content[0] == {
        "type": "video_url",
        "video_url": {"url": VIDEO_URL, "fps": 1.0},
    }
    assert content[1]["type"] == "text"
    assert content[1]["text"] == "请概括这段新闻视频。"


@pytest.mark.asyncio
async def test_fps_can_be_omitted_for_gateways_that_reject_it() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=SUCCESS_BODY)

    model = make_model(handler, send_fps=False)
    await model.summarize_video(tenant_id="tenant-1", request=make_request())

    video_block = captured[0]["messages"][0]["content"][0]["video_url"]
    assert video_block == {"url": VIDEO_URL}


def test_build_payload_is_deterministic() -> None:
    payload = HunyuanVisionVideoRpc.build_payload(
        VideoUnderstandingCallRequest(
            model_route=MODEL,
            video_url=VIDEO_URL,
            prompt="p",
            prompt_version="video-summary-v1",
            fps=2.0,
            idempotency_key="k-1",
        ),
        model=MODEL,
    )

    assert payload["messages"][0]["content"][0]["video_url"]["fps"] == 2.0


@pytest.mark.asyncio
async def test_rate_limit_becomes_retryable_degradation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="too many requests")

    model = make_model(handler)

    with pytest.raises(VideoSummaryUnavailableError) as excinfo:
        await model.summarize_video(tenant_id="tenant-1", request=make_request())

    assert excinfo.value.retryable is True
    assert "EnterpriseRpcRateLimitError" in str(excinfo.value)


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    model = make_model(handler)

    with pytest.raises(VideoSummaryUnavailableError) as excinfo:
        await model.summarize_video(tenant_id="tenant-1", request=make_request())

    assert excinfo.value.retryable is False
    assert "EnterpriseRpcAuthenticationError" in str(excinfo.value)


@pytest.mark.asyncio
async def test_missing_choices_surfaces_as_response_error_then_degrades() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "model": MODEL})

    client = HunyuanVisionVideoRpc(
        base_url="https://tokenhub.tencentmaas.test/v1",
        api_key="k",
        model=MODEL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    context = DefaultRpcCallContextProvider().create(
        tenant_id="tenant-1", operation="video-news-summarization", timeout_ms=5_000
    )

    with pytest.raises(EnterpriseRpcResponseError, match="choices"):
        await client.invoke_video_understanding(
            context=context,
            request=VideoUnderstandingCallRequest(
                model_route=MODEL,
                video_url=VIDEO_URL,
                prompt="p",
                prompt_version="video-summary-v1",
                idempotency_key="k-1",
            ),
        )


@pytest.mark.asyncio
async def test_blank_summary_is_rejected_as_unavailable() -> None:
    body = json.loads(json.dumps(SUCCESS_BODY))
    body["choices"][0]["message"]["content"] = "   "

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    model = make_model(handler)

    with pytest.raises(VideoSummaryUnavailableError, match="contract|empty"):
        await model.summarize_video(tenant_id="tenant-1", request=make_request())


@pytest.mark.asyncio
async def test_idempotency_key_is_stable_for_same_video_and_policy() -> None:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SUCCESS_BODY)

    client = HunyuanVisionVideoRpc(
        base_url="https://tokenhub.tencentmaas.test/v1",
        api_key="k",
        model=MODEL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    provider = DefaultRpcCallContextProvider()

    first = provider.create(
        tenant_id="tenant-1",
        operation="video-news-summarization",
        timeout_ms=5_000,
    )
    second = provider.create(
        tenant_id="tenant-1",
        operation="video-news-summarization",
        timeout_ms=5_000,
    )
    # 幂等键由 news_id + prompt 版本 + fps + 输出上限派生，
    # 与 request_id 无关，因此两次独立调用会得到相同的业务幂等键。
    assert RpcVideoUnderstandingModel._idempotency_key(make_request()) == (
        RpcVideoUnderstandingModel._idempotency_key(make_request())
    )
    assert first.idempotency_key is None and second.idempotency_key is None
    assert keys == []


@pytest.mark.asyncio
async def test_transport_failure_degrades_instead_of_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure", request=request)

    model = make_model(handler)

    with pytest.raises(VideoSummaryUnavailableError) as excinfo:
        await model.summarize_video(tenant_id="tenant-1", request=make_request())

    assert excinfo.value.retryable is True
