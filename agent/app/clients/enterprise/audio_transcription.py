"""企业语音识别（ASR）调用契约。

与 ``multimodal_gateway.py``（视频理解）、``model_gateway.py``（文本结构化）
并列的第三条模型侧通道。之所以单独一份契约，是因为 ASR 的**输入是音视频地址、
输出是带时间戳的长文本**，与"给定 prompt 返回概括"的形态并不相同。

通道无关
--------

契约不假设网关背后是混元、腾讯云语音识别还是其他厂商。企业侧可路由到：

- 腾讯云语音识别「录音文件识别」（``CreateRecTask``）：音频格式白名单含
  ``mp4`` / ``flv``，可直接吃视频地址，URL 最长 5 小时。
- 腾讯云 MPS「智能字幕」（``ProcessMedia``）：底层即 Hy ASR 3.0 preview。
- 混元 ASR（Hy-ASR-3.0-preview）：**当前内测版仅支持实时、单次 ≤1 分钟、
  16k 单声道 PCM**，用于长视频需要先切片再流式送，不适合作为首选通道。

``media_url`` 必须可被网关直接访问。企业内网地址需要网关侧代理或转存；
本项目不下载、不转存、不保存音视频本体。
"""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.clients.enterprise.common import (
    NonBlank128,
    NonBlank256,
    RpcCallContext,
    RpcResponseMeta,
)


class AudioTranscriptionCallRequest(BaseModel):
    """提交给企业 ASR 通道的版本化转写请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scene: Literal["news_audio_transcription"] = "news_audio_transcription"
    model_route: NonBlank128
    media_url: NonBlank256
    language: str = Field(default="zh", min_length=2, max_length=16)
    hotwords: tuple[str, ...] = ()
    max_duration_seconds: int | None = Field(default=None, gt=0, le=21_600)
    idempotency_key: NonBlank256


class AudioTranscriptionCallSegment(BaseModel):
    """带时间戳的片段，便于把检索结果溯源回视频位置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    text: str = Field(min_length=1, max_length=2_000)


class AudioTranscriptionCallUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audio_seconds: float = Field(default=0, ge=0)


class AudioTranscriptionCallResponse(BaseModel):
    """ASR 通道返回的转写结果。

    文本仍是不可信数据，必须继续经过领域层清洗与最小长度校验后才会进入知识库。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=200_000)
    model_name: NonBlank128
    model_version: NonBlank128
    language: str = Field(default="", max_length=16)
    duration_seconds: float | None = Field(default=None, ge=0)
    segments: tuple[AudioTranscriptionCallSegment, ...] = ()
    usage: AudioTranscriptionCallUsage
    meta: RpcResponseMeta


class AudioTranscriptionGatewayRpc(Protocol):
    """企业 ASR 通道的 RPC Port，不包含业务重试策略。"""

    async def invoke_audio_transcription(
        self,
        *,
        context: RpcCallContext,
        request: AudioTranscriptionCallRequest,
    ) -> AudioTranscriptionCallResponse: ...
