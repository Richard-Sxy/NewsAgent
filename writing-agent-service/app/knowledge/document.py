"""知识库文档模型与写入 Port。

与 ``app/clients/enterprise/README.md`` 的分层一致：本模块只描述"要入库什么"和
"入库端长什么样"，不感知 FastGPT、企业检索平台或其他具体实现。

一个 ``KnowledgeDocument`` 对应知识库里的一个 Collection（一个可被向量化切片的单元），
``document_id`` 就是稳定的业务主键，重复入库时用于幂等判定。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Protocol

from app.analytics.video_textualization import ContentTextualization


class IngestStatus(StrEnum):
    """单条文档的入库结果。"""

    CREATED = "created"    # 新建
    UPDATED = "updated"    # 覆盖重建
    SKIPPED = "skipped"    # 已存在且策略为跳过
    FAILED = "failed"      # 写入失败，详情见 detail


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """一个可向量化的知识库文档单元。"""

    document_id: str
    title: str
    text: str
    media_type: str
    text_source: str
    is_low_confidence: bool = False
    source_url: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.document_id.strip():
            raise ValueError("document_id cannot be empty")
        if not self.title.strip():
            raise ValueError("title cannot be empty")
        if not self.text.strip():
            raise ValueError("text cannot be empty")
        if not self.media_type.strip():
            raise ValueError("media_type cannot be empty")
        if not self.text_source.strip():
            raise ValueError("text_source cannot be empty")
        for key, value in self.metadata.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("metadata keys must be non-empty strings")
            if not isinstance(value, str):
                raise TypeError(
                    f"metadata value for {key!r} must be a string, "
                    f"got {type(value).__name__}"
                )


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    document_id: str
    status: IngestStatus
    collection_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class IngestReport:
    """一次批量入库的结果；``outcomes`` 与输入 ``documents`` 一一对应且同序。"""

    outcomes: tuple[IngestOutcome, ...] = ()

    def _select(self, status: IngestStatus) -> tuple[IngestOutcome, ...]:
        return tuple(item for item in self.outcomes if item.status is status)

    @property
    def created(self) -> tuple[IngestOutcome, ...]:
        return self._select(IngestStatus.CREATED)

    @property
    def updated(self) -> tuple[IngestOutcome, ...]:
        return self._select(IngestStatus.UPDATED)

    @property
    def skipped(self) -> tuple[IngestOutcome, ...]:
        return self._select(IngestStatus.SKIPPED)

    @property
    def failed(self) -> tuple[IngestOutcome, ...]:
        return self._select(IngestStatus.FAILED)

    @property
    def written(self) -> int:
        """真正写入知识库的条数（新建 + 覆盖）。"""

        return len(self.created) + len(self.updated)

    @property
    def is_clean(self) -> bool:
        """没有任何失败，可安全标记该批入库成功。"""

        return not self.failed


class KnowledgeBaseWriter(Protocol):
    """知识库写入 Port。

    实现约定：

    - ``outcomes`` 必须与 ``documents`` 一一对应并保持顺序；单条失败以
      ``IngestStatus.FAILED`` 返回，不中断整批。
    - 只有"整批不可恢复"的错误（鉴权失败、数据集不存在、传输层失败）才抛异常，
      让 Temporal 决定重试。
    - 同一 ``document_id`` 重复写入必须幂等：要么覆盖，要么按策略跳过，
      不能产生重复 Collection。
    """

    async def upsert_documents(
        self,
        *,
        tenant_id: str,
        documents: tuple[KnowledgeDocument, ...],
    ) -> IngestReport: ...


class InMemoryKnowledgeBaseWriter:
    """本地测试与离线演示用的写入端。"""

    def __init__(self) -> None:
        self._documents: dict[str, KnowledgeDocument] = {}

    @property
    def documents(self) -> tuple[KnowledgeDocument, ...]:
        return tuple(self._documents.values())

    def get(self, document_id: str) -> KnowledgeDocument | None:
        return self._documents.get(document_id)

    async def upsert_documents(
        self,
        *,
        tenant_id: str,
        documents: tuple[KnowledgeDocument, ...],
    ) -> IngestReport:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")

        outcomes: list[IngestOutcome] = []
        for document in documents:
            document.validate()
            existed = document.document_id in self._documents
            self._documents[document.document_id] = document
            outcomes.append(
                IngestOutcome(
                    document_id=document.document_id,
                    status=(
                        IngestStatus.UPDATED if existed else IngestStatus.CREATED
                    ),
                    collection_id=f"in-memory:{document.document_id}",
                )
            )
        return IngestReport(outcomes=tuple(outcomes))


def build_document_metadata(
    *,
    textualization: ContentTextualization,
    publish_time: str,
    source_url: str,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构造扁平字符串 metadata，供 FastGPT / 检索平台按类型过滤与降权。

    检索侧现有约定至少要 ``news_id``、``original_title``、``source_url``、
    ``publish_time``（见 ``app/analytics/README.md``）；这里再补入内容类型与
    文本来源，使"视频条目"可被单独筛选，也能对降级文档降权。
    """

    metadata: dict[str, str] = {
        "source": "writing-agent-service",
        "news_id": textualization.news_id,
        "media_type": str(textualization.media_type),
        "text_source": str(textualization.text_source),
        "text_sources": ",".join(
            str(item) for item in textualization.effective_sources
        ),
        "text_source_count": str(len(textualization.effective_sources)),
        "textualization_policy_version": textualization.policy_version,
        "is_low_confidence": (
            "true" if not textualization.is_sufficient else "false"
        ),
        "publish_time": publish_time,
        "source_url": source_url,
    }
    if textualization.model_name:
        metadata["video_model"] = textualization.model_name
    if textualization.model_version:
        metadata["video_model_version"] = textualization.model_version
    if textualization.asr_model_name:
        metadata["asr_model"] = textualization.asr_model_name
    if textualization.asr_model_version:
        metadata["asr_model_version"] = textualization.asr_model_version
    if textualization.asr_duration_seconds is not None:
        metadata["audio_duration_seconds"] = (
            f"{textualization.asr_duration_seconds:.2f}"
        )
    if textualization.degrade_reason:
        metadata["text_degrade_reason"] = textualization.degrade_reason[:400]
    if extra:
        metadata.update({key: value for key, value in extra.items()})
    return metadata
