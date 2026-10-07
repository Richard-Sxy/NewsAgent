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

超长视频处理
------------

``faster-whisper`` 解码时会把**整段音频一次性读进内存**（约 230MB/小时），
几小时的视频存在 OOM 风险。因此本实现按 ``chunk_seconds``（默认 600 秒）
用 ``ffmpeg`` 把音轨切成 16kHz 单声道 wav 分片，逐片转写，再把分片时间戳
平移合并：

- 视频不长于 ``chunk_seconds`` 时直接转写，完全不引入 ffmpeg。
- 超过阈值时走分片；``ffmpeg`` / ``ffprobe`` 不可用时回退为整段直转，
  宁可慢也不失败。
- ``vad_filter=True`` 交给 faster-whisper 跳过静音段，减少无谓解码。
- ``max_processing_seconds`` 是硬上限，超长素材只处理前 N 秒。
- 分片临时目录用完即删，不落业务磁盘。

分片按固定间隔切，可能切断跨片语句；需要逐字对齐时请用云侧带时间戳的通道。
"""

from dataclasses import dataclass, field
import asyncio
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from app.analytics.audio_transcription import (
    AudioTranscriber,
    AudioTranscription,
    AudioTranscriptionRequest,
    AudioTranscriptionUnavailableError,
    TranscriptSegment,
)

_REMOTE_SCHEMES = ("http://", "https://", "rtmp://", "rtsp://")
_FILE_SCHEME = "file://"
_CHUNK_PATTERN = "chunk_%05d.wav"


def _resolve_local_path(media_url: str) -> Path | None:
    """把 ``file://`` 或本地路径解析为 ``Path``；远程地址返回 ``None``。"""

    value = media_url.strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered.startswith(_REMOTE_SCHEMES):
        return None
    if lowered.startswith(_FILE_SCHEME):
        value = value[len(_FILE_SCHEME) :]
    path = Path(value)
    return path if path.exists() else None


def build_ffmpeg_segment_command(
    *,
    ffmpeg_binary: str,
    source: Path,
    output_pattern: Path,
    chunk_seconds: float,
    limit_seconds: float | None = None,
) -> list[str]:
    """构造"抽音轨 + 按固定时长分片"的 ffmpeg 命令（纯函数，便于单测）。

    输出统一为 16kHz 单声道 wav，``-reset_timestamps 1`` 让每个分片从 0 开始，
    由调用方按分片真实时长累加偏移量。
    """

    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(source),
    ]
    if limit_seconds is not None:
        command += ["-t", f"{max(0.0, float(limit_seconds)):.3f}"]
    command += [
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "segment",
        "-segment_time",
        f"{float(chunk_seconds):.3f}",
        "-reset_timestamps",
        "1",
        str(output_pattern),
    ]
    return command


@dataclass
class LocalWhisperTranscriber(AudioTranscriber):
    """基于 faster-whisper 的本地转写实现。"""

    model_size: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    model_name: str = "faster-whisper"
    download_root: str | None = None
    max_transcript_chars: int = 200_000
    chunk_seconds: float = 600.0
    max_processing_seconds: float | None = None
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    ffmpeg_timeout_seconds: float = 3600.0
    beam_size: int = 5
    vad_filter: bool = True
    _model: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_transcript_chars <= 0:
            raise ValueError("max_transcript_chars must be greater than 0")
        if self.chunk_seconds < 0:
            raise ValueError("chunk_seconds cannot be negative")
        if self.max_processing_seconds is not None and self.max_processing_seconds <= 0:
            raise ValueError("max_processing_seconds must be positive when set")
        if self.beam_size <= 0:
            raise ValueError("beam_size must be greater than 0")

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
        except Exception as exc:
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
        total_duration = self._probe_duration(path)
        if total_duration is None and request.max_duration_seconds is not None:
            total_duration = float(request.max_duration_seconds)

        units, workdir = self._plan_work_units(path, total_duration)
        try:
            collected: list[TranscriptSegment] = []
            parts: list[str] = []
            detected_language = ""
            measured_duration = 0.0
            has_measured_duration = False
            for unit_path, offset in units:
                segments_iter, info = self._invoke_model(model, unit_path, request)
                for segment in segments_iter:
                    text = str(segment.text).strip()
                    if not text:
                        continue
                    parts.append(text)
                    collected.append(
                        TranscriptSegment(
                            start_seconds=float(segment.start) + offset,
                            end_seconds=float(segment.end) + offset,
                            text=text,
                        )
                    )
                info_duration = getattr(info, "duration", None)
                if isinstance(info_duration, (int, float)):
                    measured_duration += float(info_duration)
                    has_measured_duration = True
                detected_language = (
                    str(getattr(info, "language", "") or "") or detected_language
                )
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)

        full_text = "".join(parts).strip()
        if not full_text:
            raise AudioTranscriptionUnavailableError(
                "local whisper produced an empty transcript",
                retryable=False,
            )
        if len(full_text) > self.max_transcript_chars:
            full_text = full_text[: self.max_transcript_chars].rstrip()

        duration: float | None
        if has_measured_duration:
            duration = measured_duration
        else:
            duration = total_duration

        return AudioTranscription(
            text=full_text,
            model_name=self.model_name,
            model_version=f"{self.model_size}/{self.compute_type}",
            language=detected_language or request.language,
            duration_seconds=duration,
            segments=tuple(collected),
        )

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise AudioTranscriptionUnavailableError(
                "faster_whisper is not installed; install it or use a cloud "
                "ASR channel instead",
                retryable=False,
            ) from exc
        self._model = WhisperModel(
            self.model_size,
            device=self.device,
            compute_type=self.compute_type,
            download_root=self.download_root,
        )
        return self._model

    def _invoke_model(
        self,
        model: Any,
        path: Path,
        request: AudioTranscriptionRequest,
    ) -> tuple[Any, Any]:
        kwargs: dict[str, Any] = {
            "language": request.language or None,
            "beam_size": self.beam_size,
            "vad_filter": self.vad_filter,
        }
        if request.hotwords:
            kwargs["hotwords"] = " ".join(request.hotwords)
        return model.transcribe(str(path), **kwargs)

    def _plan_work_units(
        self,
        path: Path,
        total_duration: float | None,
    ) -> tuple[list[tuple[Path, float]], Path | None]:
        """决定"整段直转"还是"ffmpeg 分片"。

        返回 ``(工作单元, 临时目录)``；临时目录由调用方在转写结束后清理。
        """

        if self.chunk_seconds <= 0:
            return [(path, 0.0)], None
        within_chunk = (
            total_duration is not None and total_duration <= self.chunk_seconds
        )
        within_cap = (
            self.max_processing_seconds is None
            or (total_duration is not None and total_duration <= self.max_processing_seconds)
        )
        if within_chunk and within_cap:
            return [(path, 0.0)], None

        workdir = Path(tempfile.mkdtemp(prefix="local-whisper-"))
        try:
            units = self._segment_audio(path, workdir)
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            # ffmpeg/ffprobe 不可用时宁可整段直转，也不让整条入库失败。
            return [(path, 0.0)], None
        if not units:
            shutil.rmtree(workdir, ignore_errors=True)
            return [(path, 0.0)], None
        return units, workdir

    def _segment_audio(
        self,
        path: Path,
        workdir: Path,
    ) -> list[tuple[Path, float]]:
        output_pattern = workdir / _CHUNK_PATTERN
        command = build_ffmpeg_segment_command(
            ffmpeg_binary=self.ffmpeg_binary,
            source=path,
            output_pattern=output_pattern,
            chunk_seconds=self.chunk_seconds,
            limit_seconds=self.max_processing_seconds,
        )
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=self.ffmpeg_timeout_seconds,
        )

        units: list[tuple[Path, float]] = []
        offset = 0.0
        for chunk in sorted(workdir.glob("chunk_*.wav")):
            units.append((chunk, offset))
            chunk_duration = self._probe_duration(chunk)
            offset += (
                chunk_duration
                if chunk_duration is not None
                else float(self.chunk_seconds)
            )
        return units

    def _probe_duration(self, path: Path) -> float | None:
        """用 ffprobe 读取真实时长（秒）；不可用时返回 ``None``。"""

        try:
            completed = subprocess.run(
                [
                    self.ffprobe_binary,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        try:
            duration = float(str(completed.stdout).strip())
        except (TypeError, ValueError):
            return None
        return duration if duration > 0 else None

    async def close(self) -> None:
        self._model = None
