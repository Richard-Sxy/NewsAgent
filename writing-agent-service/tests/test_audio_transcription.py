"""语音转写（ASR）接入与统一输出骨架的离线测试。

全部离线：ASR 用 ``InMemoryAudioTranscriber`` 或本地 Fake，
模型用 Fake，不触碰任何网络与云凭据。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.analytics.audio_transcription import (
    AudioTranscriber,
    AudioTranscription,
    AudioTranscriptionRequest,
    AudioTranscriptionUnavailableError,
    InMemoryAudioTranscriber,
)
from app.analytics.entities import ContentType
from app.analytics.news_content import NewsContent
from app.analytics.video_textualization import (
    TextSource,
    VideoAsset,
    VideoContentTextualizer,
    VideoSummary,
    VideoSummaryRequest,
    VideoTextualizationPolicy,
)

SHANGHAI = timezone(timedelta(hours=8))
PUBLISH_TIME = datetime(2026, 9, 10, 20, 30, tzinfo=SHANGHAI)

SPOKEN = (
    "腾讯新闻今日要闻。受强冷空气影响，北方多地气温骤降，"
    "气象部门发布寒潮蓝色预警。专家提醒公众注意防寒保暖，减少户外活动时间。"
    "交通运输部门已启动应急预案，确保道路通行安全。"
)
VISUAL = (
    "画面显示城市道路被积雪覆盖，行人在风雪中前行，"
    "路面积水结冰，能见度明显下降。"
)


class FakeAsr(AudioTranscriber):
    """记录调用参数的 ASR 替身。"""

    def __init__(
        self,
        *,
        text: str = SPOKEN,
        error: Exception | None = None,
        duration_seconds: float | None = 20.0,
    ) -> None:
        self._text = text
        self._error = error
        self._duration = duration_seconds
        self.requests: list[tuple[str, AudioTranscriptionRequest]] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def transcribe(
        self,
        *,
        tenant_id: str,
        request: AudioTranscriptionRequest,
    ) -> AudioTranscription:
        self.requests.append((tenant_id, request))
        if self._error is not None:
            raise self._error
        return AudioTranscription(
            text=self._text,
            model_name="tencent-asr-rec-task",
            model_version="16k_zh_en",
            language=request.language,
            duration_seconds=self._duration,
        )


class FakeVideoModel:
    def __init__(self, *, summary: str = VISUAL) -> None:
        self._summary = summary
        self.calls = 0

    async def summarize_video(
        self,
        *,
        tenant_id: str,
        request: VideoSummaryRequest,
    ) -> VideoSummary:
        self.calls += 1
        return VideoSummary(
            text=self._summary,
            model_name="hunyuan-turbos-vision-video",
            model_version="hunyuan-turbos-vision-video-20250728",
        )


def make_policy(**overrides) -> VideoTextualizationPolicy:
    params = {
        "version": "video-text-v2",
        "model_route": "hunyuan-turbos-vision-video-20250728",
        "min_text_chars": 20,
        "asr_min_text_chars": 20,
    }
    params.update(overrides)
    return VideoTextualizationPolicy(**params)


def make_content(**overrides) -> NewsContent:
    params = {
        "news_id": "20260910V00002",
        "title": "寒潮来袭北方多地气温骤降",
        "summary": "",
        "content_type": ContentType.VIDEO,
        "publish_time": PUBLISH_TIME,
        "source_url": "https://news.example.com/omn/20260910V00002",
    }
    params.update(overrides)
    return NewsContent(**params)


def make_asset(**overrides) -> VideoAsset:
    params = {
        "video_url": "https://video.example.com/20260910V00002.mp4",
        "duration_seconds": 20,
    }
    params.update(overrides)
    return VideoAsset(**params)


def make_textualizer(*, asr=None, model=None, **policy_overrides):
    return VideoContentTextualizer(
        policy=make_policy(**policy_overrides),
        model=model if model is not None else FakeVideoModel(),
        transcriber=asr,
    )


# ----------------------------------------------------------------------
# ASR 接入降级链
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asr_fills_transcript_when_asset_has_none() -> None:
    asr = FakeAsr()
    model = FakeVideoModel()
    textualizer = make_textualizer(asr=asr, model=model)

    result = await textualizer.textualize(
        tenant_id="tenant-1",
        content=make_content(),
        video_asset=make_asset(),
    )

    assert result.text_source is TextSource.TRANSCRIPT
    assert result.sources == (TextSource.TRANSCRIPT,)
    assert asr.calls == 1
    # 拿到口播后默认不再为同一段视频付第二次模型费用。
    assert model.calls == 0
    assert result.asr_model_name == "tencent-asr-rec-task"
    assert result.asr_model_version == "16k_zh_en"
    assert result.asr_duration_seconds == 20.0
    assert SPOKEN[:20] in result.text


@pytest.mark.asyncio
async def test_preset_transcript_skips_asr_call() -> None:
    asr = FakeAsr()
    textualizer = make_textualizer(asr=asr)

    result = await textualizer.textualize(
        tenant_id="tenant-1",
        content=make_content(),
        video_asset=make_asset(transcript=SPOKEN),
    )

    assert result.text_source is TextSource.TRANSCRIPT
    assert asr.calls == 0


@pytest.mark.asyncio
async def test_asr_unavailable_degrades_to_visual_summary() -> None:
    asr = FakeAsr(
        error=AudioTranscriptionUnavailableError("asr gateway 503", retryable=True)
    )
    model = FakeVideoModel()
    textualizer = make_textualizer(asr=asr, model=model)

    result = await textualizer.textualize(
        tenant_id="tenant-1",
        content=make_content(),
        video_asset=make_asset(),
    )

    assert result.text_source is TextSource.VIDEO_SUMMARY
    assert result.is_sufficient is True
    assert result.degrade_reason is not None
    assert "asr gateway 503" in result.degrade_reason
    assert model.calls == 1


@pytest.mark.asyncio
async def test_asr_short_text_is_rejected_by_quality_gate() -> None:
    asr = FakeAsr(text="太短了")
    model = FakeVideoModel()
    textualizer = make_textualizer(asr=asr, model=model)

    result = await textualizer.textualize(
        tenant_id="tenant-1",
        content=make_content(),
        video_asset=make_asset(),
    )

    assert result.text_source is TextSource.VIDEO_SUMMARY
    assert result.degrade_reason is not None
    assert "quality gate" in result.degrade_reason


@pytest.mark.asyncio
async def test_asr_disabled_falls_back_to_visual_summary() -> None:
    asr = FakeAsr()
    model = FakeVideoModel()
    textualizer = make_textualizer(
        asr=asr, model=model, enable_audio_transcription=False
    )

    result = await textualizer.textualize(
        tenant_id="tenant-1",
        content=make_content(),
        video_asset=make_asset(),
    )

    assert asr.calls == 0
    assert result.text_source is TextSource.VIDEO_SUMMARY


# ----------------------------------------------------------------------
# 多来源融合与统一骨架
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fuse_visual_summary_merges_transcript_and_visual() -> None:
    asr = FakeAsr()
    model = FakeVideoModel()
    textualizer = make_textualizer(
        asr=asr, model=model, fuse_visual_summary=True
    )

    result = await textualizer.textualize(
        tenant_id="tenant-1",
        content=make_content(),
        video_asset=make_asset(),
    )

    # 主来源仍是最可靠的口播，但两路来源都进文档。
    assert result.text_source is TextSource.TRANSCRIPT
    assert result.sources == (TextSource.TRANSCRIPT, TextSource.VIDEO_SUMMARY)
    assert result.is_fused is True
    assert model.calls == 1
    assert "## 语音转写" in result.text
    assert "## 画面概括" in result.text
    # 章节顺序固定，保证入库文档结构一致。
    assert result.text.index("## 语音转写") < result.text.index("## 画面概括")


@pytest.mark.asyncio
async def test_article_and_video_share_the_same_document_skeleton() -> None:
    """统一输出的核心断言：图文与视频的骨架字段完全一致。"""

    textualizer = make_textualizer()
    article = await textualizer.textualize(
        tenant_id="tenant-1",
        content=make_content(
            content_type=ContentType.ARTICLE,
            body="这是一篇图文新闻的正文，长度足够进入正文来源分支。",
        ),
        video_asset=None,
    )
    video = await textualizer.textualize(
        tenant_id="tenant-1",
        content=make_content(),
        video_asset=make_asset(transcript=SPOKEN),
    )

    common_fields = (
        "内容类型: ",
        "发布时间: ",
        "来源: ",
        "文本来源: ",
        "内容置信度: ",
    )
    for document in (article.text, video.text):
        assert document.startswith("# 寒潮来袭北方多地气温骤降")
        lines = document.splitlines()
        for field in common_fields:
            assert any(line.startswith(field) for line in lines), field

    assert "## 正文" in article.text
    assert "## 语音转写" in video.text
    assert "内容类型: 图文新闻" in article.text
    assert "内容类型: 视频新闻" in video.text
    # 视频多一行时长，但仍保留同一套头部字段。
    assert "视频时长: 20 秒" in video.text


@pytest.mark.asyncio
async def test_video_without_any_source_is_low_confidence_metadata() -> None:
    textualizer = VideoContentTextualizer(
        policy=make_policy(enable_video_understanding=False),
        model=None,
        transcriber=None,
    )

    result = await textualizer.textualize(
        tenant_id="tenant-1",
        content=make_content(summary=""),
        video_asset=make_asset(),
    )

    assert result.text_source is TextSource.METADATA
    assert result.is_sufficient is False
    assert "内容置信度: 低（已降级）" in result.text
    assert "仅元数据（降级）" in result.text


# ----------------------------------------------------------------------
# 领域对象与替身
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_memory_transcriber_returns_canned_text() -> None:
    transcriber = InMemoryAudioTranscriber(
        transcripts={"20260910V00002": SPOKEN}
    )

    result = await transcriber.transcribe(
        tenant_id="tenant-1",
        request=AudioTranscriptionRequest(
            news_id="20260910V00002",
            title="标题",
            media_url="https://video.example.com/a.mp4",
            language="zh",
        ),
    )

    assert result.text == SPOKEN
    assert result.model_name == "in-memory-asr"


@pytest.mark.asyncio
async def test_in_memory_transcriber_raises_for_unknown_news_id() -> None:
    transcriber = InMemoryAudioTranscriber(transcripts={})

    with pytest.raises(AudioTranscriptionUnavailableError) as excinfo:
        await transcriber.transcribe(
            tenant_id="tenant-1",
            request=AudioTranscriptionRequest(
                news_id="missing",
                title="标题",
                media_url="https://video.example.com/a.mp4",
                language="zh",
            ),
        )

    assert excinfo.value.retryable is False


def test_audio_transcription_rejects_blank_text() -> None:
    with pytest.raises(ValueError, match="text cannot be empty"):
        AudioTranscription(
            text="   ",
            model_name="asr",
            model_version="v1",
        ).validate()


def test_policy_rejects_invalid_asr_language() -> None:
    with pytest.raises(ValueError, match="asr_language"):
        VideoTextualizationPolicy(
            version="v1",
            model_route="route",
            asr_language="  ",
        ).validate()
