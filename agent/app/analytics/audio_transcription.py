"""语音转写（ASR）：把视频音轨转成可入库文本的领域能力。

为什么单独一层
--------------

视频新闻的"可向量化文本"主要来自口播语音。多模态视频概括（抽帧）只能描述
画面，拿不到"说了什么"；ASR 正好补上这一块。两者是互补关系，不是替代关系，
domain 层允许它们同时生效、融合成同一份文档。

通道选择（重要事实，别踩坑）
----------------------------

- **混元 ASR（Hy-ASR-3.0-preview，内测版）当前只支持实时语音识别**：单次音频
  必须 1 分钟以内、16k 单声道 PCM，且不支持 VAD / 话者分离。它<不适合>直接吃
  一条完整新闻视频，硬用就得先切片再流式送，工程成本高。
- 长视频更适合走**腾讯云语音识别「录音文件识别」**（``CreateRecTask``）：音频
  格式白名单里包含 ``mp4`` / ``flv``，也就是**可以直接把视频地址丢进去**，
  URL 方式最长 5 小时、单文件 1GB，服务端自己抽音轨。
- 或走**腾讯云 MPS「智能字幕」**（``ProcessMedia``，底层即 Hy ASR 3.0 preview）。

因此本模块只定义 Port 与领域对象，具体通道由 ``app/clients`` 下的 Adapter 决定，
换通道不影响调用方。

设计约束（与 ``AGENTS.md`` / ``PROJECT_CONTEXT.md`` 对齐）
--------------------------------------------------------

- 不下载、不转存视频本体：只把可访问的地址交给 ASR 通道。
- ASR 文本属于外部不可信输入，必须经 ``sanitize_model_text`` 清洗后才入库。
- ASR 不可用时不抛异常打断入库，而是沿降级链回落到画面概括 / 摘要 / 元数据。
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """带时间戳的转写片段，用于把检索结果溯源回视频位置。"""

    start_seconds: float
    end_seconds: float
    text: str

    def validate(self) -> None:
        if self.start_seconds < 0:
            raise ValueError("start_seconds cannot be negative")
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds cannot be earlier than start_seconds")
        if not self.text.strip():
            raise ValueError("segment text cannot be empty")


@dataclass(frozen=True, slots=True)
class AudioTranscriptionRequest:
    """一次语音转写的领域请求，已不含任何企业字段。"""

    news_id: str
    title: str
    media_url: str
    language: str
    hotwords: tuple[str, ...] = ()
    max_duration_seconds: int | None = None

    def validate(self) -> None:
        if not self.news_id.strip():
            raise ValueError("news_id cannot be empty")
        if not self.media_url.strip():
            raise ValueError("media_url cannot be empty")
        if not self.language.strip():
            raise ValueError("language cannot be empty")
        if self.max_duration_seconds is not None and self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive when set")


@dataclass(frozen=True, slots=True)
class AudioTranscription:
    """ASR 结果及其版本证据。"""

    text: str
    model_name: str
    model_version: str
    language: str = ""
    duration_seconds: float | None = None
    segments: tuple[TranscriptSegment, ...] = ()
    speaker_count: int | None = None

    def validate(self) -> None:
        if not self.text.strip():
            raise ValueError("transcription text cannot be empty")
        if not self.model_name.strip():
            raise ValueError("model_name cannot be empty")
        for segment in self.segments:
            segment.validate()

    @property
    def char_count(self) -> int:
        return len(self.text.strip())

    @property
    def has_timestamps(self) -> bool:
        return bool(self.segments)


class AudioTranscriptionUnavailableError(RuntimeError):
    """ASR 通道不可用或返回不可用结果。

    与 ``VideoSummaryUnavailableError`` 同构：领域层捕获它并沿降级链回落，
    表示"这次转写拿不到"，而不是"整条入库失败"。企业 Adapter 应把传输层错误
    翻译成本异常，并透传原始 ``retryable`` 语义。
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class AudioTranscriber(Protocol):
    """语音转写领域 Port；由企业 ASR 通道 Adapter 实现。"""

    async def transcribe(
        self,
        *,
        tenant_id: str,
        request: AudioTranscriptionRequest,
    ) -> AudioTranscription: ...


@dataclass(frozen=True, slots=True)
class InMemoryAudioTranscriber:
    """离线演示与单测用的转写实现：按 ``news_id`` 返回预置文本。"""

    transcripts: dict[str, str] = field(default_factory=dict)
    model_name: str = "in-memory-asr"
    model_version: str = "v1"

    async def transcribe(
        self,
        *,
        tenant_id: str,
        request: AudioTranscriptionRequest,
    ) -> AudioTranscription:
        request.validate()
        text = self.transcripts.get(request.news_id, "").strip()
        if not text:
            raise AudioTranscriptionUnavailableError(
                f"no canned transcript for news_id={request.news_id}",
                retryable=False,
            )
        return AudioTranscription(
            text=text,
            model_name=self.model_name,
            model_version=self.model_version,
            language=request.language,
        )
