"""
切片与向量索引编排。split_text()按字符疮毒和重叠范围切片；NativeKnowledgeIndexingService 调用 Embedding、写入索引、校验写入结果并处理版本更新。
他的文档台账保存在内存中，用于本地验收。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from app.knowledge.document import (
    IngestOutcome,
    IngestReport,
    IngestStatus,
    KnowledgeDocument,
)
from app.model_runtime.core import EmbeddingPort, EmbeddingRequest
from app.retrieval.tiered_vector import ChunkVectorRecord, TieredVectorIndexPort, VectorTier


@dataclass(frozen=True, slots=True)
class IndexedDocument:
    tenant_id: str
    document_id: str
    content_version: int
    content_sha256: str
    embedding_version: str
    chunk_ids: tuple[str, ...]


def split_text(text: str, *, max_chars: int, overlap: int) -> tuple[str, ...]:
    if max_chars <= 0 or overlap < 0 or overlap >= max_chars:
        raise ValueError("chunk size/overlap are invalid")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("cannot chunk empty text")
    chunks: list[str] = []
    start = 0
    while start < len(cleaned):
        end = min(start + max_chars, len(cleaned))
        chunks.append(cleaned[start:end])
        if end == len(cleaned):
            break
        start = end - overlap
    return tuple(chunks)


class NativeKnowledgeIndexingService:
    def __init__(
        self,
        *,
        embedding: EmbeddingPort,
        index: TieredVectorIndexPort,
        embedding_version: str,
        tier: VectorTier = VectorTier.HOT,
        max_chunk_chars: int = 800,
        chunk_overlap: int = 80,
        embedding_batch_size: int = 32,
    ) -> None:
        if not embedding_version.strip():
            raise ValueError("embedding version is required")
        if max_chunk_chars <= 0 or not 0 <= chunk_overlap < max_chunk_chars:
            raise ValueError("chunking policy is invalid")
        if not 1 <= embedding_batch_size <= 2048:
            raise ValueError("embedding batch size is invalid")
        self._embedding = embedding
        self._index = index
        self._embedding_version = embedding_version
        self._tier = tier
        self._max_chars = max_chunk_chars
        self._overlap = chunk_overlap
        self._batch_size = embedding_batch_size
        self._ledger: dict[tuple[str, str], IndexedDocument] = {}

    def get_indexed(self, *, tenant_id: str, document_id: str) -> IndexedDocument | None:
        return self._ledger.get((tenant_id, document_id))

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
            news_id = document.metadata.get("news_id", "").strip()
            version_text = document.metadata.get("content_version", "")
            publish_text = document.metadata.get("publish_time", "")
            try:
                content_version = int(version_text)
                published = datetime.fromisoformat(publish_text)
                if not news_id or content_version <= 0:
                    raise ValueError("news_id and positive content_version are required")
                if published.tzinfo is None or published.utcoffset() is None:
                    raise ValueError("publish_time must be timezone-aware")
            except (TypeError, ValueError) as exc:
                outcomes.append(
                    IngestOutcome(document.document_id, IngestStatus.FAILED, detail=str(exc))
                )
                continue

            identity = (tenant_id, document.document_id)
            content_hash = sha256(
                (document.title + "\x1f" + document.text).encode("utf-8")
            ).hexdigest()
            previous = self._ledger.get(identity)
            if previous is not None:
                if (
                    previous.content_version == content_version
                    and previous.content_sha256 == content_hash
                    and previous.embedding_version == self._embedding_version
                ):
                    outcomes.append(
                        IngestOutcome(
                            document.document_id,
                            IngestStatus.SKIPPED,
                            collection_id=f"native:{document.document_id}",
                        )
                    )
                    continue
                if (
                    content_version < previous.content_version
                    or (
                        content_version == previous.content_version
                        and content_hash != previous.content_sha256
                    )
                ):
                    outcomes.append(
                        IngestOutcome(
                            document.document_id,
                            IngestStatus.FAILED,
                            detail="changed document requires a newer content_version",
                        )
                    )
                    continue

            chunks = split_text(
                document.text, max_chars=self._max_chars, overlap=self._overlap
            )
            texts = tuple(f"{document.title}\n{chunk}" for chunk in chunks)
            trace = sha256(
                f"{tenant_id}\x1f{document.document_id}\x1f{content_version}".encode("utf-8")
            ).hexdigest()
            vectors: list[tuple[float, ...]] = []
            for offset in range(0, len(texts), self._batch_size):
                batch = texts[offset:offset + self._batch_size]
                embedded = await self._embedding.embed(
                    EmbeddingRequest(
                        tenant_id=tenant_id,
                        trace_id=f"knowledge-ingest-{trace}-{offset}",
                        model_route=self._embedding_version,
                        texts=batch,
                    )
                )
                if embedded.model_version != self._embedding_version:
                    raise ValueError("document embedding version drifted")
                if len(embedded.vectors) != len(batch):
                    raise ValueError("document embedding count differs from chunks")
                vectors.extend(embedded.vectors)
            if len(vectors) != len(chunks):
                raise ValueError("document embedding count differs from chunks")
            records = tuple(
                ChunkVectorRecord(
                    chunk_id=sha256(
                        f"{tenant_id}\x1f{document.document_id}\x1f"
                        f"{content_version}\x1f{index}\x1f{self._embedding_version}".encode("utf-8")
                    ).hexdigest(),
                    tenant_id=tenant_id,
                    news_id=news_id,
                    content_version=content_version,
                    chunk_index=index,
                    embedding_version=self._embedding_version,
                    title=document.title,
                    excerpt=chunk[:1000],
                    source_url=document.source_url or None,
                    publish_time=published,
                    vector=vector,
                )
                for index, (chunk, vector) in enumerate(
                    zip(chunks, vectors, strict=True)
                )
            )
            written = await self._index.upsert(tier=self._tier, records=records)
            chunk_ids = tuple(record.chunk_id for record in records)
            present = await self._index.contains(tier=self._tier, chunk_ids=chunk_ids)
            if written != len(records) or present != frozenset(chunk_ids):
                raise RuntimeError("vector index write verification failed")
            if previous is not None:
                await self._index.delete(tier=self._tier, chunk_ids=previous.chunk_ids)
            self._ledger[identity] = IndexedDocument(
                tenant_id=tenant_id,
                document_id=document.document_id,
                content_version=content_version,
                content_sha256=content_hash,
                embedding_version=self._embedding_version,
                chunk_ids=chunk_ids,
            )
            outcomes.append(
                IngestOutcome(
                    document.document_id,
                    IngestStatus.UPDATED if previous is not None else IngestStatus.CREATED,
                    collection_id=f"native:{document.document_id}",
                )
            )
        return IngestReport(tuple(outcomes))
