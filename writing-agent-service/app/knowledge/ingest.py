"""内容入库服务：文本化 → 文档构建 → 写入知识库。

这是"视频内容总结后导入"的编排层，把领域侧的 ``VideoContentTextualizer``
与知识库侧的 ``KnowledgeBaseWriter`` 组合起来，并负责一道关键闸门：

**内容充分性闸门。** 视频页正文常常为空，若不加判断就把"标题 + URL"写成文档，
向量库里会堆满只有标题的空壳，检索时被标题党误召回。因此默认策略是
``skip_insufficient=True``：文本不达门槛的条目直接跳过并记录原因，
而不是记一次"入库成功"。
"""

from dataclasses import dataclass, field
import hashlib
import re
from typing import Protocol, Sequence

from app.analytics.entities import ContentType
from app.analytics.news_content import NewsContent
from app.analytics.video_textualization import (
    ContentTextualization,
    VideoAsset,
    VideoContentTextualizer,
)
from app.knowledge.document import (
    IngestOutcome,
    IngestReport,
    IngestStatus,
    KnowledgeBaseWriter,
    KnowledgeDocument,
    build_document_metadata,
)


class VideoAssetResolver(Protocol):
    """把 ``news_id`` 解析为视频媒体信息。

    企业侧通常用内容中心或媒资 RPC 实现；本项目只消费结果，不下载视频本体。
    返回 ``None`` 表示该内容没有可用视频地址，文本化会自动降级。
    """

    async def resolve(
        self,
        *,
        tenant_id: str,
        news_id: str,
    ) -> VideoAsset | None: ...


@dataclass(frozen=True, slots=True)
class ContentIngestPolicy:
    """可纳入 Production Bundle 的入库策略。"""

    version: str
    document_id_prefix: str = "news"
    skip_insufficient: bool = True
    min_document_chars: int = 40

    def validate(self) -> None:
        if not self.version.strip():
            raise ValueError("policy version cannot be empty")
        if not self.document_id_prefix.strip():
            raise ValueError("document_id_prefix cannot be empty")
        if self.min_document_chars < 1:
            raise ValueError("min_document_chars must be at least 1")


def build_document_id(prefix: str, news_id: str) -> str:
    """生成稳定、可复现的文档主键。

    ``news_id`` 合法时直接拼接，便于人工排查；含非法字符时退化为哈希，
    保证幂等键不会因为替换字符而互相碰撞。
    """

    raw = news_id.strip()
    normalized = re.sub(r"[^A-Za-z0-9_-]", "-", raw).strip("-_")
    if normalized and normalized == raw:
        return f"{prefix}-{normalized}"[:120]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


class KnowledgeDocumentBuilder:
    """把文本化结果装配成知识库文档；不包含任何 I/O。"""

    def build(
        self,
        *,
        document_id: str,
        content: NewsContent,
        textualization: ContentTextualization,
    ) -> KnowledgeDocument:
        metadata = build_document_metadata(
            textualization=textualization,
            publish_time=content.publish_time.isoformat(),
            source_url=content.source_url,
            extra={"original_title": content.title},
        )
        document = KnowledgeDocument(
            document_id=document_id,
            title=content.title,
            text=textualization.text,
            media_type=str(textualization.media_type),
            text_source=str(textualization.text_source),
            is_low_confidence=not textualization.is_sufficient,
            source_url=content.source_url,
            metadata=metadata,
        )
        document.validate()
        return document


@dataclass
class ContentIngestService:
    """把一批 ``NewsContent`` 文本化后写入知识库。"""

    textualizer: VideoContentTextualizer
    writer: KnowledgeBaseWriter
    policy: ContentIngestPolicy
    video_asset_resolver: VideoAssetResolver | None = None
    builder: KnowledgeDocumentBuilder = field(
        default_factory=KnowledgeDocumentBuilder
    )

    def __post_init__(self) -> None:
        self.policy.validate()

    async def ingest(
        self,
        *,
        tenant_id: str,
        contents: Sequence[NewsContent],
    ) -> IngestReport:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")

        news_ids = [content.news_id for content in contents]
        if len(news_ids) != len(set(news_ids)):
            raise ValueError("contents cannot contain duplicate news_id values")
        if not contents:
            return IngestReport()

        results: list[IngestOutcome | None] = [None] * len(contents)
        pending: list[tuple[int, KnowledgeDocument]] = []

        for index, content in enumerate(contents):
            document_id = build_document_id(
                self.policy.document_id_prefix, content.news_id
            )
            asset = await self._resolve_video_asset(
                tenant_id=tenant_id, content=content
            )
            textualization = await self.textualizer.textualize(
                tenant_id=tenant_id,
                content=content,
                video_asset=asset,
            )

            if self._should_skip(textualization):
                results[index] = IngestOutcome(
                    document_id=document_id,
                    status=IngestStatus.SKIPPED,
                    detail=self._skip_detail(textualization),
                )
                continue

            pending.append(
                (
                    index,
                    self.builder.build(
                        document_id=document_id,
                        content=content,
                        textualization=textualization,
                    ),
                )
            )

        if pending:
            report = await self.writer.upsert_documents(
                tenant_id=tenant_id,
                documents=tuple(document for _, document in pending),
            )
            if len(report.outcomes) != len(pending):
                raise ValueError(
                    "knowledge base writer must return one outcome per document"
                )
            for (index, _), outcome in zip(pending, report.outcomes):
                results[index] = outcome

        return IngestReport(
            outcomes=tuple(item for item in results if item is not None)
        )

    async def _resolve_video_asset(
        self,
        *,
        tenant_id: str,
        content: NewsContent,
    ) -> VideoAsset | None:
        if content.content_type is not ContentType.VIDEO:
            return None
        if self.video_asset_resolver is None:
            return None
        return await self.video_asset_resolver.resolve(
            tenant_id=tenant_id,
            news_id=content.news_id,
        )

    def _should_skip(self, textualization: ContentTextualization) -> bool:
        if not self.policy.skip_insufficient:
            return False
        if not textualization.is_sufficient:
            return True
        return textualization.text_length < self.policy.min_document_chars

    @staticmethod
    def _skip_detail(textualization: ContentTextualization) -> str:
        base = (
            f"insufficient text: source={textualization.text_source}, "
            f"content_chars={textualization.content_chars}, "
            f"doc_chars={textualization.text_length}"
        )
        if textualization.degrade_reason:
            return f"{base}; {textualization.degrade_reason}"[:500]
        return base
