"""``LocalWhisperTranscriber`` 的离线单测。

不安装 ``faster_whisper``、不调用真实的 ``ffmpeg`` / ``ffprobe``：
模型用注入到 ``sys.modules`` 的替身，媒体探测与分片用 monke ypatch 替身。
"""

from pathlib import Path
import sys

import pytest

from app.analytics.audio_transcription import (
    AudioTranscriptionRequest,
    AudioTranscriptionUnavailableError,
)
from app.clients.local_whisper import (
    LocalWhisperTranscriber,
    _resolve_local_path,
    build_ffmpeg_segment_command,
)


class FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class FakeInfo:
    def __init__(self, duration: float | None, language: str = "zh") -> None:
        self.duration = duration
        self.language = language


class FakeModel:
    """按调用顺序返回预置的 ``(segments, info)``，并记录每次参数。"""

    def __init__(self, responses: list[tuple[list[FakeSegment], FakeInfo]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def transcribe(self, path: str, **kwargs):
        self.calls.append((path, kwargs))
        if not self._responses:
            raise AssertionError("FakeModel received more calls than expected")
        segments, info = self._responses.pop(0)
        return iter(segments), info


def install_fake_whisper(monkeypatch, model: FakeModel) -> None:
    module = type(sys)("faster_whisper")
    module.WhisperModel = lambda *args, **kwargs: model
    monkeypatch.setitem(sys.modules, "faster_whisper", module)


def make_request(media_url: str, **overrides) -> AudioTranscriptionRequest:
    params = {
        "news_id": "20260910V00002",
        "title": "寒潮来袭",
        "media_url": media_url,
        "language": "zh",
    }
    params.update(overrides)
    return AudioTranscriptionRequest(**params)


def make_media(tmp_path: Path) -> Path:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake-media")
    return media


# ----------------------------------------------------------------------
# 路径解析
# ----------------------------------------------------------------------


def test_remote_url_is_not_a_local_path() -> None:
    for url in (
        "http://video.example.com/a.mp4",
        "https://video.example.com/a.mp4",
        "rtmp://live.example.com/a",
        "rtsp://live.example.com/a",
    ):
        assert _resolve_local_path(url) is None


def test_blank_and_missing_paths_are_rejected(tmp_path: Path) -> None:
    assert _resolve_local_path("   ") is None
    assert _resolve_local_path(str(tmp_path / "nope.mp4")) is None


def test_local_path_and_file_scheme_are_accepted(tmp_path: Path) -> None:
    media = make_media(tmp_path)
    assert _resolve_local_path(str(media)) == media
    assert _resolve_local_path(f"file://{media}") == media


# ----------------------------------------------------------------------
# 降级行为
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_url_degrades_without_loading_model(
    monkeypatch, tmp_path: Path
) -> None:
    model = FakeModel([])
    install_fake_whisper(monkeypatch, model)
    transcriber = LocalWhisperTranscriber(chunk_seconds=0)

    with pytest.raises(AudioTranscriptionUnavailableError) as excinfo:
        await transcriber.transcribe(
            tenant_id="tenant-1",
            request=make_request("https://video.example.com/a.mp4"),
        )

    assert excinfo.value.retryable is False
    assert "refusing remote media_url" in str(excinfo.value)
    assert model.calls == []


@pytest.mark.asyncio
async def test_missing_faster_whisper_degrades(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    transcriber = LocalWhisperTranscriber(chunk_seconds=0)
    monkeypatch.setattr(transcriber, "_probe_duration", lambda path: None)

    with pytest.raises(AudioTranscriptionUnavailableError) as excinfo:
        await transcriber.transcribe(
            tenant_id="tenant-1",
            request=make_request(f"file://{make_media(tmp_path)}"),
        )

    assert excinfo.value.retryable is False
    assert "faster_whisper is not installed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_empty_transcript_degrades(monkeypatch, tmp_path: Path) -> None:
    model = FakeModel([([], FakeInfo(duration=10.0))])
    install_fake_whisper(monkeypatch, model)
    transcriber = LocalWhisperTranscriber(chunk_seconds=0)
    monkeypatch.setattr(transcriber, "_probe_duration", lambda path: None)

    with pytest.raises(AudioTranscriptionUnavailableError) as excinfo:
        await transcriber.transcribe(
            tenant_id="tenant-1",
            request=make_request(f"file://{make_media(tmp_path)}"),
        )

    assert excinfo.value.retryable is False
    assert "empty transcript" in str(excinfo.value)


# ----------------------------------------------------------------------
# 短音频：整段直转
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_audio_transcribes_directly(monkeypatch, tmp_path: Path) -> None:
    model = FakeModel(
        [
            (
                [FakeSegment(0.0, 2.0, " 腾讯新闻 "), FakeSegment(2.0, 4.0, "今日要闻")],
                FakeInfo(duration=4.0, language="zh"),
            )
        ]
    )
    install_fake_whisper(monkeypatch, model)
    transcriber = LocalWhisperTranscriber(model_size="small", chunk_seconds=600)
    monkeypatch.setattr(transcriber, "_probe_duration", lambda path: 4.0)

    def fail_if_segmented(*args, **kwargs):
        raise AssertionError("short audio must not invoke ffmpeg")

    monkeypatch.setattr(transcriber, "_segment_audio", fail_if_segmented)

    result = await transcriber.transcribe(
        tenant_id="tenant-1",
        request=make_request(
            f"file://{make_media(tmp_path)}",
            hotwords=("寒潮", "气温"),
        ),
    )

    assert result.text == "腾讯新闻今日要闻"
    assert result.model_name == "faster-whisper"
    assert result.model_version == "small/int8"
    assert result.duration_seconds == 4.0
    assert [s.start_seconds for s in result.segments] == [0.0, 2.0]
    assert model.calls[0][1]["hotwords"] == "寒潮 气温"
    assert model.calls[0][1]["vad_filter"] is True


@pytest.mark.asyncio
async def test_transcript_is_truncated(monkeypatch, tmp_path: Path) -> None:
    model = FakeModel(
        [([FakeSegment(0.0, 1.0, "一二三四五六七八九十")], FakeInfo(1.0))]
    )
    install_fake_whisper(monkeypatch, model)
    transcriber = LocalWhisperTranscriber(chunk_seconds=0, max_transcript_chars=4)
    monkeypatch.setattr(transcriber, "_probe_duration", lambda path: None)

    result = await transcriber.transcribe(
        tenant_id="tenant-1",
        request=make_request(f"file://{make_media(tmp_path)}"),
    )

    assert result.text == "一二三四"


# ----------------------------------------------------------------------
# 超长音频：分片 + 时间戳平移
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_audio_is_chunked_and_offsets_are_shifted(
    monkeypatch, tmp_path: Path
) -> None:
    model = FakeModel(
        [
            ([FakeSegment(0.0, 5.0, "上半段")], FakeInfo(duration=600.0)),
            ([FakeSegment(0.0, 5.0, "下半段")], FakeInfo(duration=600.0)),
        ]
    )
    install_fake_whisper(monkeypatch, model)
    transcriber = LocalWhisperTranscriber(chunk_seconds=600)
    monkeypatch.setattr(transcriber, "_probe_duration", lambda path: 1200.0)

    def fake_segment(path, workdir):
        workdir.mkdir(parents=True, exist_ok=True)
        first = workdir / "chunk_00000.wav"
        second = workdir / "chunk_00001.wav"
        first.write_bytes(b"a")
        second.write_bytes(b"b")
        return [(first, 0.0), (second, 600.0)]

    monkeypatch.setattr(transcriber, "_segment_audio", fake_segment)

    result = await transcriber.transcribe(
        tenant_id="tenant-1",
        request=make_request(f"file://{make_media(tmp_path)}"),
    )

    assert result.text == "上半段下半段"
    assert len(model.calls) == 2
    assert [s.start_seconds for s in result.segments] == [0.0, 600.0]
    assert [s.end_seconds for s in result.segments] == [5.0, 605.0]
    assert result.duration_seconds == 1200.0


@pytest.mark.asyncio
async def test_ffmpeg_failure_falls_back_to_direct_transcription(
    monkeypatch, tmp_path: Path
) -> None:
    model = FakeModel([([FakeSegment(0.0, 1.0, "整段直转")], FakeInfo(1.0))])
    install_fake_whisper(monkeypatch, model)
    transcriber = LocalWhisperTranscriber(chunk_seconds=600)
    monkeypatch.setattr(transcriber, "_probe_duration", lambda path: 3600.0)

    def broken_segment(path, workdir):
        raise FileNotFoundError("ffmpeg not found")

    monkeypatch.setattr(transcriber, "_segment_audio", broken_segment)

    result = await transcriber.transcribe(
        tenant_id="tenant-1",
        request=make_request(f"file://{make_media(tmp_path)}"),
    )

    assert result.text == "整段直转"
    assert len(model.calls) == 1


def test_ffmpeg_command_contains_segment_and_cap() -> None:
    command = build_ffmpeg_segment_command(
        ffmpeg_binary="ffmpeg",
        source=Path("/tmp/in.mp4"),
        output_pattern=Path("/tmp/chunk_%05d.wav"),
        chunk_seconds=600.0,
        limit_seconds=1800.0,
    )
    assert command[0] == "ffmpeg"
    assert "-t" in command and command[command.index("-t") + 1] == "1800.000"
    assert command[command.index("-segment_time") + 1] == "600.000"
    assert command[command.index("-ar") + 1] == "16000"
    assert command[-1] == "/tmp/chunk_%05d.wav"


@pytest.mark.asyncio
async def test_close_releases_model(monkeypatch, tmp_path: Path) -> None:
    model = FakeModel([([FakeSegment(0.0, 1.0, "内容")], FakeInfo(1.0))])
    install_fake_whisper(monkeypatch, model)
    transcriber = LocalWhisperTranscriber(chunk_seconds=0)
    monkeypatch.setattr(transcriber, "_probe_duration", lambda path: None)

    await transcriber.transcribe(
        tenant_id="tenant-1",
        request=make_request(f"file://{make_media(tmp_path)}"),
    )
    assert transcriber._model is model
    await transcriber.close()
    assert transcriber._model is None
