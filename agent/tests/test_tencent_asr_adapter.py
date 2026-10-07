"""腾讯云 ASR（录音文件识别）通道的离线测试。

用 ``httpx.MockTransport`` 模拟 CreateRecTask + DescribeTaskStatus 的异步轮询，
不访问任何真实网络、不使用任何真实凭据。
"""

import hashlib
import json

import httpx
import pytest

from app.analytics.audio_transcription import (
    AudioTranscriber,
    AudioTranscriptionRequest,
    AudioTranscriptionUnavailableError,
    TranscriptSegment,
)
from app.clients.enterprise.audio_transcription import (
    AudioTranscriptionCallRequest,
)
from app.clients.enterprise.common import DefaultRpcCallContextProvider
from app.clients.enterprise.tencent_asr import (
    RpcAudioTranscriber,
    TencentAsrCredentials,
    TencentCloudAsrRpc,
    build_canonical_request,
    build_tc3_authorization,
)

RESULT_TEXT = (
    "同讯新闻今日要闻,受墙冷空气影响,北方多地气温皱降,"
    "气象部门发布韩朝蓝色预警。"
)


def make_call_request(**overrides) -> AudioTranscriptionCallRequest:
    params = {
        "model_route": "tencent-asr-16k-zh-en",
        "media_url": "https://video.example.com/20260910V00002.mp4",
        "language": "zh",
        "idempotency_key": "news-1|zh|https://video.example.com/a.mp4",
    }
    params.update(overrides)
    return AudioTranscriptionCallRequest(**params)


def make_context():
    return DefaultRpcCallContextProvider().create(
        tenant_id="tenant-1",
        operation="news-audio-transcription",
        timeout_ms=120_000,
    )


def test_tc3_authorization_is_deterministic_and_payload_sensitive() -> None:
    kwargs = {
        "secret_id": "AKIDEXAMPLE",
        "secret_key": "SECRETEXAMPLE",
        "action": "CreateRecTask",
        "timestamp": 1_760_000_000,
        "region": "ap-guangzhou",
    }
    first, ts = build_tc3_authorization(payload={"Url": "https://a/b.mp4"}, **kwargs)
    second, _ = build_tc3_authorization(payload={"Url": "https://a/b.mp4"}, **kwargs)
    # 签名覆盖「方法 + host + payload 哈希」，payload 变化必须导致签名变化。
    other, _ = build_tc3_authorization(payload={"Url": "https://a/c.mp4"}, **kwargs)

    assert first == second
    assert first != other
    assert ts == 1_760_000_000
    assert first.startswith("TC3-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "SignedHeaders=content-type;host" in first
    assert "Signature=" in first


def test_tc3_authorization_does_not_sign_x_tc_action() -> None:
    """``X-TC-Action`` 经请求头传递，不参与签名（与官方示例的 SignedHeaders 一致）。"""

    common = {
        "secret_id": "AKIDEXAMPLE",
        "secret_key": "SECRETEXAMPLE",
        "payload": {"TaskId": 1},
        "timestamp": 1_760_000_000,
    }
    create, _ = build_tc3_authorization(action="CreateRecTask", **common)
    describe, _ = build_tc3_authorization(action="DescribeTaskStatus", **common)
    assert create.split("Signature=")[-1] == describe.split("Signature=")[-1]


def test_tc3_authorization_signs_the_endpoint_host() -> None:
    """内网 / 自定义 endpoint 必须参与签名，否则服务端必然报签名失败。"""

    kwargs = {
        "secret_id": "AKIDEXAMPLE",
        "secret_key": "SECRETEXAMPLE",
        "action": "CreateRecTask",
        "payload": {"Url": "https://a/b.mp4"},
        "timestamp": 1_760_000_000,
    }
    public, _ = build_tc3_authorization(**kwargs)
    internal, _ = build_tc3_authorization(
        host="asr.internal.tencentcloudapi.com", **kwargs
    )
    assert public != internal


def test_tc3_authorization_requires_credentials() -> None:
    with pytest.raises(ValueError, match="secret_id"):
        build_tc3_authorization(
            secret_id="  ", secret_key="k", action="CreateRecTask", payload={}
        )


def test_build_create_task_payload_maps_language_and_hotwords() -> None:
    payload = TencentCloudAsrRpc.build_create_task_payload(
        make_call_request(hotwords=("寒潮", "气象局"))
    )

    assert payload["EngineModelType"] == "16k_zh_en"
    assert payload["SourceType"] == 0
    # 关键：SourceType=0 + Url，允许直接传 mp4，服务端自行抽音轨。
    assert payload["Url"].endswith(".mp4")
    assert payload["ChannelNum"] == 1
    assert payload["HotwordList"] == "寒潮|5,气象局|5"


def test_build_create_task_payload_defaults_engine_for_unknown_language() -> None:
    payload = TencentCloudAsrRpc.build_create_task_payload(
        make_call_request(language="xx")
    )
    assert payload["EngineModelType"] == "16k_zh_en"


def _mock_asr_transport(*, fail_status: int | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        action = request.headers.get("x-tc-action", "")
        payload = json.loads(request.content.decode("utf-8"))

        if action == "CreateRecTask":
            assert payload["Url"].endswith(".mp4")
            return httpx.Response(
                200,
                json={
                    "Response": {
                        "Data": {"TaskId": 1234567},
                        "RequestId": "req-create",
                    }
                },
            )
        if action == "DescribeTaskStatus":
            if fail_status is not None:
                return httpx.Response(
                    200,
                    json={
                        "Response": {
                            "Data": {
                                "Status": 3,
                                "ErrorMsg": "audio download failed",
                            },
                            "RequestId": "req-status",
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "Response": {
                        "Data": {
                            "Status": 2,
                            "StatusStr": "success",
                            "Result": RESULT_TEXT,
                            # 官方 TaskStatus.AudioDuration 类型 Float、单位「秒」。
                            # 这里刻意用小数，防止再被误当成毫秒换算。
                            "AudioDuration": 19.97,
                            "ResultDetail": [
                                {
                                    "StartMs": 20,
                                    "EndMs": 19_970,
                                    "FinalSentence": RESULT_TEXT,
                                }
                            ],
                        },
                        "RequestId": "req-status",
                    }
                },
            )
        raise AssertionError(f"unexpected action: {action}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_full_flow_submits_then_polls_and_parses() -> None:
    client = httpx.AsyncClient(transport=_mock_asr_transport())
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="id", secret_key="key"),
        http_client=client,
        poll_interval_seconds=0.01,
    )

    response = await rpc.invoke_audio_transcription(
        context=make_context(),
        request=make_call_request(),
    )

    assert response.text == RESULT_TEXT
    assert response.model_name == "tencent-asr-rec-task"
    assert response.model_version == "16k_zh_en"
    assert response.duration_seconds == pytest.approx(19.97)
    assert len(response.segments) == 1
    assert response.segments[0].end_seconds == pytest.approx(19.97)
    assert response.meta.source_system == "tencent-cloud-asr"
    await client.aclose()


@pytest.mark.asyncio
async def test_failed_task_is_reported_as_response_error() -> None:
    from app.clients.enterprise.common import EnterpriseRpcResponseError

    client = httpx.AsyncClient(
        transport=_mock_asr_transport(fail_status=3)
    )
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="id", secret_key="key"),
        http_client=client,
        poll_interval_seconds=0.01,
    )

    with pytest.raises(EnterpriseRpcResponseError) as excinfo:
        await rpc.invoke_audio_transcription(
            context=make_context(),
            request=make_call_request(),
        )

    assert "audio download failed" in str(excinfo.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_api_level_error_under_http_200_is_mapped() -> None:
    from app.clients.enterprise.common import EnterpriseRpcAuthenticationError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Response": {
                    "Error": {
                        "Code": "AuthFailure.SignatureFailure",
                        "Message": "signature is invalid",
                    },
                    "RequestId": "req-1",
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="id", secret_key="key"),
        http_client=client,
    )

    with pytest.raises(EnterpriseRpcAuthenticationError):
        await rpc.invoke_audio_transcription(
            context=make_context(),
            request=make_call_request(),
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_transport_503_maps_to_unavailable() -> None:
    from app.clients.enterprise.common import EnterpriseRpcUnavailableError

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, text="service unavailable")
        )
    )
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="id", secret_key="key"),
        http_client=client,
    )

    with pytest.raises(EnterpriseRpcUnavailableError):
        await rpc.invoke_audio_transcription(
            context=make_context(),
            request=make_call_request(),
        )
    await client.aclose()


# ----------------------------------------------------------------------
# 领域 Adapter：把传输层错误翻译为"可降级"语义
# ----------------------------------------------------------------------


class FailingRpc:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.requests: list[AudioTranscriptionCallRequest] = []

    async def invoke_audio_transcription(self, *, context, request):
        self.requests.append(request)
        raise self._error


class RecordingRpc:
    def __init__(self, text: str = RESULT_TEXT) -> None:
        self._text = text
        self.requests: list[AudioTranscriptionCallRequest] = []

    async def invoke_audio_transcription(self, *, context, request):
        self.requests.append(request)
        from app.clients.enterprise.audio_transcription import (
            AudioTranscriptionCallResponse,
            AudioTranscriptionCallSegment,
            AudioTranscriptionCallUsage,
        )
        from app.clients.enterprise.common import RpcResponseMeta
        from datetime import datetime, timezone

        return AudioTranscriptionCallResponse(
            text=self._text,
            model_name="tencent-asr-rec-task",
            model_version="16k_zh_en",
            language=request.language,
            duration_seconds=19.97,
            segments=(
                AudioTranscriptionCallSegment(
                    start_seconds=0.0, end_seconds=19.97, text=self._text
                ),
            ),
            usage=AudioTranscriptionCallUsage(audio_seconds=19.97),
            meta=RpcResponseMeta(
                request_id="req-1",
                source_system="tencent-cloud-asr",
                source_version="task-1",
                served_at=datetime.now(tz=timezone.utc),
            ),
        )


def make_domain_request(**overrides) -> AudioTranscriptionRequest:
    params = {
        "news_id": "20260910V00002",
        "title": "寒潮来袭",
        "media_url": "https://video.example.com/a.mp4",
        "language": "zh",
    }
    params.update(overrides)
    return AudioTranscriptionRequest(**params)


@pytest.mark.asyncio
async def test_adapter_maps_rpc_error_to_degradable_error() -> None:
    from app.clients.enterprise.common import EnterpriseRpcUnavailableError

    rpc = FailingRpc(
        EnterpriseRpcUnavailableError("gateway 503", request_id="req-1")
    )
    transcriber = RpcAudioTranscriber(rpc, model_route="tencent-asr-16k-zh-en")

    with pytest.raises(AudioTranscriptionUnavailableError) as excinfo:
        await transcriber.transcribe(
            tenant_id="tenant-1", request=make_domain_request()
        )

    assert excinfo.value.retryable is True
    assert "gateway 503" in str(excinfo.value)


@pytest.mark.asyncio
async def test_adapter_passes_media_url_and_duration_through() -> None:
    rpc = RecordingRpc()
    transcriber = RpcAudioTranscriber(rpc, model_route="tencent-asr-16k-zh-en")

    result = await transcriber.transcribe(
        tenant_id="tenant-1",
        request=make_domain_request(max_duration_seconds=30, hotwords=("寒潮",)),
    )

    assert result.text == RESULT_TEXT
    assert result.duration_seconds == pytest.approx(19.97)
    assert result.segments[0].text == RESULT_TEXT
    assert isinstance(rpc.requests[0].media_url, str)
    assert rpc.requests[0].max_duration_seconds == 30
    assert rpc.requests[0].hotwords == ("寒潮",)


@pytest.mark.asyncio
async def test_adapter_rejects_blank_transcript() -> None:
    transcriber = RpcAudioTranscriber(
        RecordingRpc(text="   "), model_route="tencent-asr-16k-zh-en"
    )

    with pytest.raises(AudioTranscriptionUnavailableError):
        await transcriber.transcribe(
            tenant_id="tenant-1", request=make_domain_request()
        )


def test_adapter_exposes_domain_port_method() -> None:
    transcriber = RpcAudioTranscriber(
        RecordingRpc(), model_route="tencent-asr-16k-zh-en"
    )
    # Protocol 不是 runtime_checkable，这里断言端口方法确实存在。
    assert callable(getattr(transcriber, "transcribe", None))


def test_transcript_segment_validates_range() -> None:
    with pytest.raises(ValueError, match="end_seconds"):
        TranscriptSegment(start_seconds=5.0, end_seconds=1.0, text="x").validate()


# ----------------------------------------------------------------------
# 官方协议事实：引擎枚举、ResTextFormat 兼容性、热词与限制
# ----------------------------------------------------------------------


def test_engine_names_match_official_casing() -> None:
    """EngineModelType 大小写敏感，必须逐字对齐官方枚举。"""

    assert (
        TencentCloudAsrRpc.build_create_task_payload(
            make_call_request(language="zh-tw")
        )["EngineModelType"]
        == "16k_zh-TW"
    )
    assert (
        TencentCloudAsrRpc.build_create_task_payload(
            make_call_request(language="en")
        )["EngineModelType"]
        == "16k_en_large"
    )


def test_res_text_format_uses_3_for_chinese_but_0_for_incompatible_engine() -> None:
    """官方限制：ja / ko 等引擎只支持 ResTextFormat=0。"""

    zh = TencentCloudAsrRpc.build_create_task_payload(make_call_request(language="zh"))
    ja = TencentCloudAsrRpc.build_create_task_payload(make_call_request(language="ja"))

    assert zh["ResTextFormat"] == 3
    assert ja["ResTextFormat"] == 0


def test_create_task_payload_sets_numeric_normalisation() -> None:
    payload = TencentCloudAsrRpc.build_create_task_payload(make_call_request())
    # 新闻入库希望数字规范化，便于检索与统计。
    assert payload["ConvertNumMode"] == 1
    assert payload["FilterPunc"] == 0
    assert "CallbackUrl" not in payload


def test_hotword_list_rejects_blank_oversized_and_separator_chars() -> None:
    from app.clients.enterprise.tencent_asr import build_hotword_list

    assert build_hotword_list(("寒潮", "气象局")) == "寒潮|5,气象局|5"
    # 去重但不改顺序
    assert build_hotword_list(("寒潮", "寒潮", "暴雨")) == "寒潮|5,暴雨|5"

    with pytest.raises(ValueError, match="at least one"):
        build_hotword_list(("  ",))
    with pytest.raises(ValueError, match="30 characters"):
        build_hotword_list(("超" * 31,))
    # 分隔符会破坏 词|权重 的结构，必须在本地拒绝而不是发给服务端
    with pytest.raises(ValueError, match="must not contain"):
        build_hotword_list(("寒潮|9",))
    with pytest.raises(ValueError, match="must not contain"):
        build_hotword_list(("寒潮,暴雨",))


def test_media_url_extension_is_checked_but_extensionless_url_passes() -> None:
    from app.clients.enterprise.tencent_asr import ensure_supported_media_url

    assert ensure_supported_media_url("https://cdn/a.mp4") == "https://cdn/a.mp4"
    assert ensure_supported_media_url("https://cdn/a.flv?token=x") == (
        "https://cdn/a.flv?token=x"
    )
    # 企业内网地址常常没有扩展名，不能误杀
    assert ensure_supported_media_url("https://cdn/20260910V00002") == (
        "https://cdn/20260910V00002"
    )
    for bad in ("https://cdn/a.avi", "https://cdn/a.mkv", "https://cdn/a.m3u8"):
        with pytest.raises(ValueError, match="unsupported media extension"):
            ensure_supported_media_url(bad)


@pytest.mark.asyncio
async def test_oversized_duration_is_rejected_before_submitting() -> None:
    client = httpx.AsyncClient(transport=_mock_asr_transport())
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="id", secret_key="key"),
        http_client=client,
        poll_interval_seconds=0.01,
    )
    with pytest.raises(ValueError, match="max_duration_seconds"):
        await rpc.submit_transcription_task(
            context=make_context(),
            request=make_call_request(max_duration_seconds=6 * 3600),
        )
    await client.aclose()


# ----------------------------------------------------------------------
# 提交 / 查询分离：长时异步任务的正解
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_and_describe_are_separate_atomic_operations() -> None:
    """两段式是生产推荐路径：提交只返回 TaskId，不阻塞等待识别完成。"""

    client = httpx.AsyncClient(transport=_mock_asr_transport())
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="id", secret_key="key"),
        http_client=client,
        poll_interval_seconds=0.01,
    )

    task_id = await rpc.submit_transcription_task(
        context=make_context(), request=make_call_request()
    )
    assert task_id == 1234567

    snapshot = await rpc.describe_transcription_task(
        task_id, request_id="req-poll"
    )
    assert snapshot.task_id == 1234567
    assert snapshot.status == 2
    assert snapshot.status_name == "success"
    assert snapshot.is_terminal is True
    assert snapshot.is_success is True
    assert snapshot.error_message == ""
    await client.aclose()


@pytest.mark.asyncio
async def test_describe_returns_non_terminal_snapshot_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        action = request.headers.get("x-tc-action", "")
        if action == "CreateRecTask":
            return httpx.Response(
                200,
                json={"Response": {"Data": {"TaskId": 1}, "RequestId": "r1"}},
            )
        return httpx.Response(
            200,
            json={
                "Response": {
                    "Data": {
                        "Status": 1,
                        "StatusStr": "doing",
                        "AudioDuration": 0,
                        "Result": "",
                        "ResultDetail": [],
                    },
                    "RequestId": "r1",
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="id", secret_key="key"),
        http_client=client,
    )

    snapshot = await rpc.describe_transcription_task(1, request_id="r1")
    assert snapshot.status == 1
    assert snapshot.status_name == "doing"
    assert snapshot.is_terminal is False
    await client.aclose()


@pytest.mark.asyncio
async def test_string_task_id_is_accepted() -> None:
    """TaskId 官方类型是 uint64；网关可能序列化成字符串，需要兼容。"""

    big = "18446744073709551615"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-tc-action") == "CreateRecTask":
            return httpx.Response(
                200, json={"Response": {"Data": {"TaskId": big}, "RequestId": "r"}}
            )
        return httpx.Response(
            200,
            json={
                "Response": {
                    "Data": {
                        "Status": 2,
                        "Result": RESULT_TEXT,
                        "AudioDuration": 1.5,
                        "ResultDetail": [],
                    },
                    "RequestId": "r",
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="id", secret_key="key"),
        http_client=client,
        poll_interval_seconds=0.01,
    )
    response = await rpc.invoke_audio_transcription(
        context=make_context(), request=make_call_request()
    )
    assert int(big) == 18446744073709551615
    assert response.duration_seconds == pytest.approx(1.5)
    await client.aclose()


@pytest.mark.asyncio
async def test_duration_falls_back_to_result_detail_when_audio_duration_missing() -> None:
    """AudioDuration 缺失时用 ResultDetail 的毫秒时间戳兜底。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-tc-action") == "CreateRecTask":
            return httpx.Response(
                200, json={"Response": {"Data": {"TaskId": 9}, "RequestId": "r"}}
            )
        return httpx.Response(
            200,
            json={
                "Response": {
                    "Data": {
                        "Status": 2,
                        "Result": RESULT_TEXT,
                        "ResultDetail": [
                            {"StartMs": 0, "EndMs": 8_000, "FinalSentence": "前半段"},
                            {"StartMs": 8_000, "EndMs": 21_500, "FinalSentence": "后半段"},
                        ],
                    },
                    "RequestId": "r",
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="id", secret_key="key"),
        http_client=client,
        poll_interval_seconds=0.01,
    )
    response = await rpc.invoke_audio_transcription(
        context=make_context(), request=make_call_request()
    )
    assert response.duration_seconds == pytest.approx(21.5)
    assert len(response.segments) == 2
    assert response.segments[1].start_seconds == pytest.approx(8.0)
    await client.aclose()


# ----------------------------------------------------------------------
# 鉴权可插拔：公网 TC3 与内网网关
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_header_signer_supports_internal_gateway() -> None:
    """腾讯内部接入：网关注入身份头，不走云 API 密钥签名。"""

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update({k.lower(): v for k, v in request.headers.items()})
        action = request.headers.get("x-tc-action", "")
        if action == "CreateRecTask":
            return httpx.Response(
                200, json={"Response": {"Data": {"TaskId": 7}, "RequestId": "r"}}
            )
        return httpx.Response(
            200,
            json={
                "Response": {
                    "Data": {
                        "Status": 2,
                        "Result": RESULT_TEXT,
                        "AudioDuration": 3.0,
                        "ResultDetail": [],
                    },
                    "RequestId": "r",
                }
            },
        )

    from app.clients.enterprise.tencent_asr import StaticHeaderSigner

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = TencentCloudAsrRpc(
        signer=StaticHeaderSigner({"X-Internal-Ticket": "ticket-abc"}),
        endpoint="http://asr.internal.tencentcloudapi.com",
        http_client=client,
        poll_interval_seconds=0.01,
    )
    response = await rpc.invoke_audio_transcription(
        context=make_context(), request=make_call_request()
    )

    assert response.text == RESULT_TEXT
    assert captured["x-internal-ticket"] == "ticket-abc"
    assert "authorization" not in captured
    await client.aclose()


def test_rpc_requires_credentials_or_signer() -> None:
    with pytest.raises(ValueError, match="credentials or signer"):
        TencentCloudAsrRpc()


def test_custom_endpoint_is_parsed_at_construction() -> None:
    from app.clients.enterprise.tencent_asr import host_of

    assert host_of("https://asr.tencentcloudapi.com") == "asr.tencentcloudapi.com"
    assert host_of("asr.internal.tencentcloudapi.com") == (
        "asr.internal.tencentcloudapi.com"
    )
    with pytest.raises(ValueError, match="cannot derive host"):
        host_of("")


@pytest.mark.asyncio
async def test_internal_error_is_mapped_to_retryable_unavailable() -> None:
    from app.clients.enterprise.common import EnterpriseRpcUnavailableError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Response": {
                    "Error": {
                        "Code": "InternalError.FailAccessDatabase",
                        "Message": "db is down",
                    },
                    "RequestId": "r",
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="id", secret_key="key"),
        http_client=client,
    )
    with pytest.raises(EnterpriseRpcUnavailableError) as excinfo:
        await rpc.submit_transcription_task(
            context=make_context(), request=make_call_request()
        )
    assert excinfo.value.retryable is True
    await client.aclose()


@pytest.mark.asyncio
async def test_callback_url_is_sent_when_configured() -> None:
    """配置了回调地址就带上 CallbackUrl，生产可省掉轮询。"""

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200, json={"Response": {"Data": {"TaskId": 1}, "RequestId": "r"}}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="id", secret_key="key"),
        http_client=client,
        callback_url="https://internal.example.com/asr/callback",
    )
    await rpc.submit_transcription_task(
        context=make_context(), request=make_call_request()
    )
    assert seen["CallbackUrl"] == "https://internal.example.com/asr/callback"
    await client.aclose()


# ----------------------------------------------------------------------
# 用官方文档示例值复算：证明 TC3 拼接格式正确，而不是"看起来像对的"
# ----------------------------------------------------------------------

# 腾讯云「签名方法 v3」文档示例用的 payload 原文（紧凑写法仅出现在此字符串里；
# 官方原文是带空格且中文转义的形态，这里逐字保留）。
_OFFICIAL_PAYLOAD_JSON = (
    '{"Limit": 1, "Filters": [{"Values": ["\\u672a\\u547d\\u540d"], '
    '"Name": "instance-name"}]}'
)
_OFFICIAL_HASHED_PAYLOAD = (
    "35e9c5b0e3ae67532d3c9f17ead6c90222632e5b1ff7f6e89887f1398934f064"
)
_OFFICIAL_HASHED_CANONICAL = (
    "7019a55be8395899b900fb5564e4200d984910f34794a27cb3fb7d10ff6a1e84"
)


def test_canonical_request_reproduces_official_document_hash() -> None:
    """照官方示例（CVM DescribeInstances）复算，哈希必须逐位一致。

    这一条把"拼接格式"钉死了 —— 尤其是 query string 为空时那一行空行，
    以及 canonical headers 每行结尾的换行。
    """

    assert (
        hashlib.sha256(_OFFICIAL_PAYLOAD_JSON.encode("utf-8")).hexdigest()
        == _OFFICIAL_HASHED_PAYLOAD
    )

    canonical = build_canonical_request(
        action="DescribeInstances",
        payload_json=_OFFICIAL_PAYLOAD_JSON,
        host="cvm.tencentcloudapi.com",
        signed_headers=("content-type", "host", "x-tc-action"),
    )
    assert (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        == _OFFICIAL_HASHED_CANONICAL
    )
    # 空行结构：方法 / 空 query / canonical headers / 空行 / signed headers
    assert canonical.splitlines()[1] == "/"
    assert canonical.splitlines()[2] == ""


@pytest.mark.asyncio
async def test_signature_covers_the_exact_bytes_that_are_sent() -> None:
    """签名覆盖的 payload 必须等于实际发出的 body，否则服务端必然拒签。"""

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        captured["authorization"] = request.headers["authorization"]
        captured["timestamp"] = request.headers["x-tc-timestamp"]
        return httpx.Response(
            200, json={"Response": {"Data": {"TaskId": 1}, "RequestId": "r"}}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = TencentCloudAsrRpc(
        credentials=TencentAsrCredentials(secret_id="AKIDX", secret_key="SECRETX"),
        http_client=client,
    )
    await rpc.submit_transcription_task(
        context=make_context(), request=make_call_request()
    )

    rebuilt, _ = build_tc3_authorization(
        secret_id="AKIDX",
        secret_key="SECRETX",
        action="CreateRecTask",
        payload=json.loads(captured["body"]),
        host="asr.tencentcloudapi.com",
        timestamp=int(captured["timestamp"]),
    )
    assert rebuilt == captured["authorization"]
    await client.aclose()


