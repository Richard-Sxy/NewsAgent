"""本地语音转写：基于 faster-whisper 的离线参考实现。

用途定位
--------

这是 ``AudioTranscriber`` 的**离线参考实现**，与 ``TencentCloudAsrRpc`` 同一层次，
但解决不同问题：

- 企业 ASR 网关 / 腾讯云凭据尚未就绪时，用它做端到端链路验证与单测联调。
- 需要"数据不出内网"的兜底通道时，可用它替代云侧 ASR。

它**不是**生产首选：本地推理有延迟与并发瓶颈，且模型权重需要另行获取。
生产应优先把转写放在企业侧（内容中心直接给字幕，或统一 ASR 网关）。

设计约束
--------

- ``faster_whisper`` 是可选依赖，采用懒加载：未安装时抛领域降级异常，
  而不是在 import 阶段炸掉整个服务。
- 只接受**本地路径或 ``file://`` 地址**。本项目不下载、不转存视频本体，
  远程 URL 一律拒绝——那是企业网关该做的事。
- 模型加载与推理都是阻塞操作，统一放进线程池，避免卡住事件循环。
"""

from dataclasses import dataclass, field
import asyncio
from pathlib import Path
from typing import Any

from app.analytics.audio_transcription import (
    AudioTranscriber,
    AudioTranscription,
    AudioTranscriptionRequest,
    AudioTranscriptionUnavailableError,
    TranscriptSegment,
)


def _resolve_local_path(media_url: str) -> Path | None:
    """把 ``file://`` 或本地路径解析为 ``Path``；远程地址返回 ``None``。"""

    value = media_url.strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "rtmp://", "rtsp://")):
        return None
    if lowered.startswith("file://"):
        value = value[len("file://") :]
    path = Path(value)
    return path if path.exists() else None


@dataclass
class LocalWhisperTranscriber(AudioTranscriber):
    """基于 faster-whisper 的本地转写实现。"""

    model_size: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    model_name: str = "faster-whisper"
    download_root: str | None = None
    max_transcript_chars: int = 200_000
    _model: Any = field(default=None, init=False, repr=False)

    async def transcribe(
        self,
        *,
        tenant_id: str,
        request: AudioTranscriptionRequest,
    ) -> AudioTranscription:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        request.validate()

        path = _resolve_local_path(request.media_url)
        if path is None:
            raise AudioTranscriptionUnavailableError(
                "local whisper requires a local file path or file:// URL; "
                f"refusing remote media_url={request.media_url[:120]!r}",
                retryable=False,
            )

        try:
            return await asyncio.to_thread(self._transcribe_sync, path, request)
        except AudioTranscriptionUnavailableError:
            raise
        except Exception as exc:  # pragma: no cover - 依赖运行环境
            raise AudioTranscriptionUnavailableError(
                f"local whisper transcription failed: {type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc

    def _transcribe_sync(
        self,
        path: Path,
        request: AudioTranscriptionRequest,
    ) -> AudioTranscription:
        model = self._load_model()
        segments_iter, info = model.transcribe(
            str(path),
            language=request.language or None,
            beam_size=5,
            vad_filter=True,
            **(
                {"hotwords": " ".join(request.hotwords)}
                if request.hotwords
                else {}
            ),
        )

        collected: list[TranscriptSegment] = []
        parts: list[str] = []
        for segment in segments_iter:
            text = str(segment.text).strip()
            if not text:
                continue
            parts.append(text)
            collected.append(
                TranscriptSegment(
                    start_seconds=float(segment.start),
                    end_seconds=float(segment.end),
                    text=text,
                )
            )

        full_text = "".join(parts).strip()
        if not full_text:
            raise AudioTranscriptionUnavailableError(
                "local whisper produced an empty transcript",
                retryable=False,
            )
        if len(full_text) > self.max_transcript_chars:
            full_text = full_text[: self.max_transcript_chars].rstrip()

        duration = getattr(info, "duration", None)
        return AudioTranscription(
            text=full_text,
            model_name=self.model_name,
            model_version=f"{self.model_size}/{self.compute_type}",
            language=str(getattr(info, "language", "") or request.language),
            duration_seconds=(
                float(duration) if isinstance(duration, (int, float)) else None
            ),
            segments=tuple(collected),
        )

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - 可选依赖
            raise AudioTranscriptionUnavailableError(
                "faster_whisper is not installed; install it or use a cloud ASR "
                "channel instead",
                retryable=False,
            ) from exc
        self._model = WhisperModel(
            self.model_size,
            device=self.device,
            compute_type=self.compute_type,
            download_root=self.download_root,
        )
        return self._model

    async def close(self) -> None:
        self._model = None
