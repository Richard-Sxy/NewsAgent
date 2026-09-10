"""视频内容入库链路的集中装配。

调用链（自上游到下游）：

```text
企业内容中心 RPC（已有 RpcNewsContentRepository）
→ NewsContent（article / video）
→ ContentIngestService.ingest
   ├─ VideoAssetResolver.resolve            （企业媒资 RPC，可选）
   ├─ VideoContentTextualizer.textualize    （领域策略 + 降级链 + 统一骨架）
   │  ├─ AudioTranscriber.transcribe         （语音转写：说了什么）
   │  │  └─ RpcAudioTranscriber              （企业 ASR 网关 Adapter）
   │  │     └─ AudioTranscriptionGatewayRpc  （企业 ASR Port）
   │  │        └─ TencentCloudAsrRpc         （腾讯云录音文件识别，联调用）
   │  └─ VideoUnderstandingModel.summarize_video  （画面概括：拍到了什么）
   │     └─ RpcVideoUnderstandingModel       （企业模型网关 Adapter）
   │        └─ MultimodalModelGatewayRpc     （企业统一网关 Port）
   │           └─ HunyuanVisionVideoRpc      （混元协议参考实现，联调用）
   ├─ KnowledgeDocumentBuilder.build        （文档 + 元数据）
   └─ KnowledgeBaseWriter.upsert_documents
      └─ FastGPTKnowledgeWriter             （FastGPT 数据集）
```

默认 **fail-closed**：``VIDEO_INGEST_ENABLED`` 未显式打开、或模型地址与凭据缺失时，
本函数直接抛错，不会在未配置的情况下对外发起模型调用。
"""

from dataclasses import dataclass, field

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.analytics.audio_transcription import AudioTranscriber
from app.analytics.video_textualization import (
    DEFAULT_VIDEO_SUMMARY_PROMPT,
    VideoContentTextualizer,
    VideoSummaryPrompt,
    VideoTextualizationPolicy,
    VideoUnderstandingModel,
)
from app.clients.enterprise.hunyuan_video import (
    HunyuanVisionVideoRpc,
    RpcVideoUnderstandingModel,
)
from app.clients.enterprise.tencent_asr import (
    DEFAULT_ENDPOINT as _DEFAULT_ASR_ENDPOINT,
    NoAuthSigner,
    RpcAudioTranscriber,
    StaticHeaderSigner,
    TencentAsrCredentials,
    TencentCloudAsrRpc,
)
from app.clients.knowledge_writer import (
    FastGPTKnowledgeWriter,
    KnowledgeWriterConfig,
)
from app.knowledge.document import KnowledgeBaseWriter
from app.knowledge.ingest import (
    ContentIngestPolicy,
    ContentIngestService,
    VideoAssetResolver,
)


class VideoIngestSettings(BaseSettings):
    """视频入库链路配置，与主 ``Settings`` 解耦，默认全部关闭。"""

    video_ingest_enabled: bool = False
    video_understanding_enabled: bool = True

    # 企业统一模型网关地址与凭据；生产环境应由 Secret 注入。
    video_model_base_url: str = ""
    video_model_api_key: str = ""
    video_model_route: str = "hunyuan-turbos-vision-video-20250728"

    # fps 是成本与粒度总开关：越低越省 token，产出越接近概览级摘要。
    video_summary_fps: float = 1.0
    video_summary_max_output_chars: int = 600

    # 语音转写（ASR）：默认关闭，打开后视频优先取口播文本。
    # 混元 ASR 内测版仅支持实时、单次 ≤1 分钟，长视频请走腾讯云
    # 「录音文件识别」（CreateRecTask，可直接传 mp4 地址）。
    video_asr_enabled: bool = False
    video_asr_route: str = "tencent-asr-16k-zh-en"
    video_asr_language: str = "zh"
    video_asr_hotwords: tuple[str, ...] = ()
    video_asr_timeout_ms: int = 120_000

    # 鉴权形态：tc3=公网云 API 密钥；gateway=内网网关注入头；none=内网免签。
    # 腾讯内部接入通常用 gateway 或 none，并把 endpoint 换成内网域名。
    video_asr_auth_mode: str = "tc3"
    video_asr_secret_id: str = ""
    video_asr_secret_key: str = ""
    video_asr_region: str = ""
    video_asr_endpoint: str = ""
    # 仅 gateway 模式使用；由 Secret 注入，不落日志。
    video_asr_gateway_headers: dict[str, str] = {}
    # 配置后走回调模式，生产可省掉轮询。
    video_asr_callback_url: str = ""
    # 扩展名不在官方白名单时直接拒绝（企业内网无后缀地址仍放行）。
    video_asr_strict_media_format: bool = True
    # 便捷入口的轮询参数；生产建议改用提交/查询两段式编排。
    video_asr_poll_interval_seconds: float = 3.0
    video_asr_max_poll_attempts: int = 100

    # 默认只在没有口播文本时才调画面模型，避免为同一段视频付两次费用。
    video_fuse_visual_summary: bool = False

    video_text_policy_version: str = "video-text-v1"
    video_text_min_chars: int = 40
    video_ingest_skip_insufficient: bool = True
    video_ingest_timeout_ms: int = 120_000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@dataclass
class VideoIngestRuntime:
    """装配完成的视频入库运行时。"""

    textualizer: VideoContentTextualizer
    writer: KnowledgeBaseWriter
    service: ContentIngestService
    model_client: object | None = field(default=None, repr=False)
    asr_client: object | None = field(default=None, repr=False)
    _owns_model_client: bool = field(default=False, repr=False)
    _owns_asr_client: bool = field(default=False, repr=False)

    async def close(self) -> None:
        for owns, client in (
            (self._owns_model_client, self.model_client),
            (self._owns_asr_client, self.asr_client),
        ):
            if owns and client is not None:
                close = getattr(client, "close", None)
                if close is not None:
                    await close()
        writer_close = getattr(self.writer, "close", None)
        if writer_close is not None:
            await writer_close()


def build_video_textualization_policy(
    settings: VideoIngestSettings,
    *,
    prompt: VideoSummaryPrompt | None = None,
) -> VideoTextualizationPolicy:
    """由配置构造可版本化的视频文本化策略。"""

    policy = VideoTextualizationPolicy(
        version=settings.video_text_policy_version,
        model_route=settings.video_model_route,
        prompt=prompt or DEFAULT_VIDEO_SUMMARY_PROMPT,
        fps=settings.video_summary_fps,
        max_output_chars=settings.video_summary_max_output_chars,
        min_text_chars=settings.video_text_min_chars,
        enable_video_understanding=settings.video_understanding_enabled,
        enable_audio_transcription=settings.video_asr_enabled,
        fuse_visual_summary=settings.video_fuse_visual_summary,
        asr_min_text_chars=settings.video_text_min_chars,
        asr_language=settings.video_asr_language,
        asr_hotwords=tuple(settings.video_asr_hotwords),
    )
    policy.validate()
    return policy


def build_asr_rpc(settings: VideoIngestSettings) -> TencentCloudAsrRpc:
    """按鉴权形态装配腾讯云「录音文件识别」客户端。

    - ``tc3``：公网云 API 密钥（SecretId / SecretKey）。
    - ``gateway``：腾讯内部 / 企业网关注入身份头，调用方只负责携带。
    - ``none``：内网免签。

    凭据与网关头一律从配置注入，不写死、不进日志。缺项时直接抛错（fail-closed），
    避免在未配置的情况下对外发起计费请求。
    """

    mode = settings.video_asr_auth_mode.strip().lower()
    endpoint = settings.video_asr_endpoint.strip() or None
    callback_url = settings.video_asr_callback_url.strip() or None

    credentials: TencentAsrCredentials | None = None
    signer: object | None = None

    if mode == "tc3":
        if not settings.video_asr_secret_id.strip():
            raise ValueError(
                "VIDEO_ASR_SECRET_ID is required when VIDEO_ASR_AUTH_MODE=tc3"
            )
        if not settings.video_asr_secret_key.strip():
            raise ValueError(
                "VIDEO_ASR_SECRET_KEY is required when VIDEO_ASR_AUTH_MODE=tc3"
            )
        credentials = TencentAsrCredentials(
            secret_id=settings.video_asr_secret_id,
            secret_key=settings.video_asr_secret_key,
            region=settings.video_asr_region,
            endpoint=endpoint or _DEFAULT_ASR_ENDPOINT,
        )
    elif mode == "gateway":
        if not settings.video_asr_gateway_headers:
            raise ValueError(
                "VIDEO_ASR_GATEWAY_HEADERS is required when "
                "VIDEO_ASR_AUTH_MODE=gateway"
            )
        if not endpoint:
            raise ValueError(
                "VIDEO_ASR_ENDPOINT is required when VIDEO_ASR_AUTH_MODE=gateway"
            )
        signer = StaticHeaderSigner(settings.video_asr_gateway_headers)
    elif mode == "none":
        if not endpoint:
            raise ValueError(
                "VIDEO_ASR_ENDPOINT is required when VIDEO_ASR_AUTH_MODE=none"
            )
        signer = NoAuthSigner()
    else:
        raise ValueError(f"unsupported VIDEO_ASR_AUTH_MODE: {mode!r}")

    return TencentCloudAsrRpc(
        credentials=credentials,
        signer=signer,  # type: ignore[arg-type]
        endpoint=endpoint,
        callback_url=callback_url,
        strict_media_format=settings.video_asr_strict_media_format,
        poll_interval_seconds=settings.video_asr_poll_interval_seconds,
        max_poll_attempts=settings.video_asr_max_poll_attempts,
    )


def create_video_ingest_runtime(
    settings: VideoIngestSettings,
    *,
    fastgpt_settings: KnowledgeWriterConfig,
    video_asset_resolver: VideoAssetResolver | None = None,
    model_override: VideoUnderstandingModel | None = None,
    transcriber_override: AudioTranscriber | None = None,
    writer_override: KnowledgeBaseWriter | None = None,
    prompt: VideoSummaryPrompt | None = None,
) -> VideoIngestRuntime:
    """装配视频入库运行时。

    测试或特殊环境可用 ``model_override`` / ``transcriber_override`` /
    ``writer_override`` 注入替身，避免访问模型网关、ASR 通道与 FastGPT。

    默认 **fail-closed**：``VIDEO_ASR_ENABLED`` 打开但凭据缺失时直接抛错，
    不会在未配置的情况下对外发起转写请求。
    """

    if not settings.video_ingest_enabled:
        raise ValueError(
            "VIDEO_INGEST_ENABLED is false; refusing to build the video ingest runtime"
        )

    policy = build_video_textualization_policy(settings, prompt=prompt)

    model_client: object | None = None
    owns_model_client = False
    model = model_override
    if model is None and policy.enable_video_understanding:
        if not settings.video_model_base_url.strip():
            raise ValueError("VIDEO_MODEL_BASE_URL is required for video understanding")
        if not settings.video_model_api_key.strip():
            raise ValueError("VIDEO_MODEL_API_KEY is required for video understanding")
        model_client = HunyuanVisionVideoRpc(
            base_url=settings.video_model_base_url,
            api_key=settings.video_model_api_key,
            model=settings.video_model_route,
        )
        owns_model_client = True
        model = RpcVideoUnderstandingModel(
            model_client,  # type: ignore[arg-type]
            model_route=settings.video_model_route,
            timeout_ms=settings.video_ingest_timeout_ms,
        )

    asr_client: object | None = None
    owns_asr_client = False
    transcriber = transcriber_override
    if transcriber is None and policy.enable_audio_transcription:
        asr_client = build_asr_rpc(settings)
        owns_asr_client = True
        transcriber = RpcAudioTranscriber(
            asr_client,  # type: ignore[arg-type]
            model_route=settings.video_asr_route,
            timeout_ms=settings.video_asr_timeout_ms,
        )

    textualizer = VideoContentTextualizer(
        policy=policy, model=model, transcriber=transcriber
    )
    writer = writer_override or FastGPTKnowledgeWriter(fastgpt_settings)
    ingest_policy = ContentIngestPolicy(
        version=f"{settings.video_text_policy_version}-ingest",
        skip_insufficient=settings.video_ingest_skip_insufficient,
        min_document_chars=settings.video_text_min_chars,
    )
    ingest_policy.validate()

    service = ContentIngestService(
        textualizer=textualizer,
        writer=writer,
        policy=ingest_policy,
        video_asset_resolver=video_asset_resolver,
    )
    return VideoIngestRuntime(
        textualizer=textualizer,
        writer=writer,
        service=service,
        model_client=model_client,
        asr_client=asr_client,
        _owns_model_client=owns_model_client,
        _owns_asr_client=owns_asr_client,
    )
