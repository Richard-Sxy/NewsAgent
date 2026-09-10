"""视频入库编排与写入端的测试。

覆盖三件事：

1. 入库编排：文本化 → 文档 → 写入，且 metadata 带上内容类型。
2. 充分性闸门：文本不达门槛时跳过而不是写入空壳。
3. FastGPT 写入端：幂等跳过、payload 形态、错误归类。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest

from app.analytics.entities import ContentType
from app.analytics.news_content import NewsContent
from app.analytics.video_textualization import (
    VideoAsset,
    VideoContentTextualizer,
    VideoSummary,
    VideoTextualizationPolicy,
)
from app.clients.knowledge_writer import FastGPTKnowledgeWriter
from app.knowledge.document import (
    InMemoryKnowledgeBaseWriter,
    IngestReport,
    IngestStatus,
    KnowledgeDocument,
)
from app.knowledge.ingest import (
    ContentIngestPolicy,
    ContentIngestService,
    build_document_id,
)

SHANGHAI = timezone(timedelta(hours=8))
PUBLISH_TIME = datetime(2026, 9, 10, 20, 30, tzinfo=SHANGHAI)
GOOD_SUMMARY = (
    "画面显示沿海城市出现强风暴雨，路面积水明显。"
    "气象部门称台风已于当晚登陆，预计未来两天仍有持续降雨。"
)


class FakeVideoModel:
    def __init__(self, summary: str = GOOD_SUMMARY) -> None:
        self._summary = summary
        self.calls = 0

    async def summarize_video(self, *, tenant_id: str, request) -> VideoSummary:
        self.calls += 1
        return VideoSummary(
            text=self._summary,
            model_name="hunyuan-turbos-vision-video",
            model_version="hunyuan-turbos-vision-video-20250728",
            total_tokens=846,
        )


@dataclass
class FakeVideoAssetResolver:
    assets: dict[str, VideoAsset] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def resolve(self, *, tenant_id: str, news_id: str) -> VideoAsset | None:
        self.calls.append(news_id)
        return self.assets.get(news_id)


def make_video_content(news_id: str = "20260910V00001", **overrides) -> NewsContent:
    params = {
        "news_id": news_id,
        "title": "台风登陆沿海地区并带来强降雨",
        "summary": "中央气象台发布台风红色预警，预计沿海地区将出现持续强降雨。",
        "content_type": ContentType.VIDEO,
        "publish_time": PUBLISH_TIME,
        "source_url": f"https://news.example.com/omn/{news_id}",
    }
    params.update(overrides)
    return NewsContent(**params)


def make_service(
    *,
    writer=None,
    resolver=None,
    policy: ContentIngestPolicy | None = None,
    model: FakeVideoModel | None = None,
) -> tuple[ContentIngestService, InMemoryKnowledgeBaseWriter]:
    resolved_writer = writer if writer is not None else InMemoryKnowledgeBaseWriter()
    textualizer = VideoContentTextualizer(
        policy=VideoTextualizationPolicy(
            version="video-text-v1",
            model_route="hunyuan-turbos-vision-video-20250728",
            min_text_chars=20,
        ),
        model=model or FakeVideoModel(),
    )
    service = ContentIngestService(
        textualizer=textualizer,
        writer=resolved_writer,
        policy=policy
        or ContentIngestPolicy(version="video-ingest-v1", min_document_chars=20),
        video_asset_resolver=resolver,
    )
    return service, resolved_writer


@pytest.mark.asyncio
async def test_video_ingest_writes_document_with_media_metadata() -> None:
    resolver = FakeVideoAssetResolver(
        assets={"20260910V00001": VideoAsset(video_url="https://v.example.com/a.mp4", duration_seconds=92)}
    )
    service, writer = make_service(resolver=resolver)

    report = await service.ingest(
        tenant_id="tenant-1", contents=[make_video_content()]
    )

    assert report.is_clean is True
    assert len(report.created) == 1
    assert resolver.calls == ["20260910V00001"]

    document = writer.get("news-20260910V00001")
    assert document is not None
    assert document.media_type == "video"
    assert document.text_source == "video_summary"
    assert document.metadata["news_id"] == "20260910V00001"
    assert document.metadata["media_type"] == "video"
    assert document.metadata["text_source"] == "video_summary"
    assert document.metadata["video_model"] == "hunyuan-turbos-vision-video"
    assert document.metadata["is_low_confidence"] == "false"
    assert "## 画面概括" in document.text


@pytest.mark.asyncio
async def test_insufficient_video_is_skipped_not_ingested() -> None:
    service, writer = make_service()

    report = await service.ingest(
        tenant_id="tenant-1",
        contents=[make_video_content(summary="")],
    )

    assert len(report.skipped) == 1
    assert report.written == 0
    assert writer.documents == ()
    assert "insufficient text" in (report.skipped[0].detail or "")


@pytest.mark.asyncio
async def test_skip_disabled_still_ingests_but_flags_low_confidence() -> None:
    service, writer = make_service(
        policy=ContentIngestPolicy(
            version="video-ingest-v1",
            skip_insufficient=False,
            min_document_chars=20,
        )
    )

    report = await service.ingest(
        tenant_id="tenant-1",
        contents=[make_video_content(summary="")],
    )

    assert report.written == 1
    document = writer.get("news-20260910V00001")
    assert document is not None
    assert document.is_low_confidence is True
    assert document.metadata["is_low_confidence"] == "true"


@pytest.mark.asyncio
async def test_writer_must_return_one_outcome_per_document() -> None:
    class BrokenWriter:
        async def upsert_documents(self, *, tenant_id, documents):
            return IngestReport()

    service, _ = make_service(writer=BrokenWriter())

    with pytest.raises(ValueError, match="one outcome per document"):
        await service.ingest(tenant_id="tenant-1", contents=[make_video_content()])


@pytest.mark.asyncio
async def test_duplicate_news_ids_are_rejected() -> None:
    service, _ = make_service()
    content = make_video_content()

    with pytest.raises(ValueError, match="duplicate news_id"):
        await service.ingest(tenant_id="tenant-1", contents=[content, content])


def test_build_document_id_is_stable_and_collision_safe() -> None:
    assert build_document_id("news", "20260910V00001") == "news-20260910V00001"
    # 含非法字符时退化为哈希，保证不同 news_id 不会映射到同一个键
    first = build_document_id("news", "台风/登陆")
    second = build_document_id("news", "台风-登陆")
    assert first.startswith("news-")
    assert first != second


# ----------------------------------------------------------------------
# FastGPT 写入端
# ----------------------------------------------------------------------


def make_fastgpt_settings() -> SimpleNamespace:
    return SimpleNamespace(
        fastgpt_base_url="http://fastgpt.test",
        fastgpt_api_key="secret",
        fastgpt_dataset_id="dataset-1",
    )


def make_document(document_id: str = "news-20260910V00001") -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=document_id,
        title="台风登陆沿海地区",
        text="# 台风登陆沿海地区\n\n内容类型: 视频新闻\n",
        media_type="video",
        text_source="video_summary",
        metadata={"news_id": "20260910V00001", "media_type": "video"},
    )


@pytest.mark.asyncio
async def test_fastgpt_writer_creates_collection_with_metadata() -> None:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path.endswith("/collection/list"):
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {"pageNum": 1, "pageSize": 20, "data": [], "total": 0},
                },
            )
        return httpx.Response(
            200, json={"code": 200, "data": {"collectionId": "col-new"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        writer = FastGPTKnowledgeWriter(make_fastgpt_settings(), http_client=client)
        report = await writer.upsert_documents(
            tenant_id="tenant-1", documents=(make_document(),)
        )

    assert report.created[0].collection_id == "col-new"
    create_path, create_payload = calls[-1]
    assert create_path.endswith("/collection/create/text")
    assert create_payload["datasetId"] == "dataset-1"
    assert create_payload["name"] == "news-20260910V00001"
    assert create_payload["trainingType"] == "chunk"
    assert create_payload["chunkSettingMode"] == "auto"
    assert create_payload["metadata"]["media_type"] == "video"


@pytest.mark.asyncio
async def test_fastgpt_writer_skips_existing_collection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/collection/list"):
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "pageNum": 1,
                        "pageSize": 20,
                        "data": [
                            {"_id": "col-existing", "name": "news-20260910V00001"}
                        ],
                        "total": 1,
                    },
                },
            )
        raise AssertionError("create should not be called for an existing document")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        writer = FastGPTKnowledgeWriter(make_fastgpt_settings(), http_client=client)
        report = await writer.upsert_documents(
            tenant_id="tenant-1", documents=(make_document(),)
        )

    assert report.skipped[0].collection_id == "col-existing"
    assert report.created == ()


@pytest.mark.asyncio
async def test_fastgpt_writer_marks_failed_without_aborting_batch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/collection/list"):
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {"pageNum": 1, "pageSize": 20, "data": [], "total": 0},
                },
            )
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        writer = FastGPTKnowledgeWriter(make_fastgpt_settings(), http_client=client)
        report = await writer.upsert_documents(
            tenant_id="tenant-1",
            documents=(make_document("news-a"), make_document("news-b")),
        )

    assert len(report.failed) == 2
    assert report.is_clean is False
    assert "FastGPTServerError" in (report.failed[0].detail or "")


@pytest.mark.asyncio
async def test_fastgpt_writer_raises_when_dataset_id_missing() -> None:
    settings = make_fastgpt_settings()
    settings.fastgpt_dataset_id = None

    with pytest.raises(ValueError, match="FASTGPT_DATASET_ID"):
        FastGPTKnowledgeWriter(settings)


@pytest.mark.asyncio
async def test_ingest_status_values_are_stable() -> None:
    assert IngestStatus.CREATED == "created"
    assert IngestStatus.SKIPPED == "skipped"
