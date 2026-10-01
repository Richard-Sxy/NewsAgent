"""Milvus implementation of the tiered vector index port.

Collections are expected to share the schema documented by
``MilvusTieredVectorIndex.REQUIRED_FIELDS``.  ``pymilvus`` is imported only by
``connect`` so unit tests and non-Milvus workers do not need the dependency.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from app.retrieval.tiered_vector import (
    ChunkVectorRecord,
    VectorSearchHit,
    VectorTier,
)


class MilvusTieredVectorIndex:
    REQUIRED_FIELDS = (
        "chunk_id",
        "tenant_id",
        "news_id",
        "content_version",
        "chunk_index",
        "embedding_version",
        "title",
        "excerpt",
        "source_url",
        "publish_time_epoch",
        "vector",
    )
    _SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9_.:@/-]{1,256}$")

    def __init__(
        self,
        client: Any,
        *,
        collections: Mapping[VectorTier, str],
        metric_type: str = "IP",
        search_params: Mapping[str, Any] | None = None,
    ) -> None:
        missing = set(VectorTier) - set(collections)
        if missing:
            raise ValueError(f"missing Milvus collections for tiers: {missing}")
        if any(not name.strip() for name in collections.values()):
            raise ValueError("Milvus collection names cannot be empty")
        self._client = client
        self._collections = dict(collections)
        self._metric_type = metric_type
        self._search_params = dict(search_params or {"ef": 96})

    @classmethod
    def connect(
        cls,
        *,
        uri: str,
        token: str | None,
        database: str,
        collections: Mapping[VectorTier, str],
        timeout_seconds: float = 10.0,
    ) -> "MilvusTieredVectorIndex":
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "pymilvus is required; install the 'milvus' extra"
            ) from exc
        client = MilvusClient(
            uri=uri,
            token=token or "",
            db_name=database,
            timeout=timeout_seconds,
        )
        return cls(client, collections=collections)

    async def search(
        self,
        *,
        tier: VectorTier,
        tenant_id: str,
        vector: Sequence[float],
        embedding_version: str,
        exclude_news_ids: frozenset[str],
        limit: int,
    ) -> list[VectorSearchHit]:
        self._validate_identity(tenant_id, "tenant_id")
        self._validate_identity(embedding_version, "embedding_version")
        for news_id in exclude_news_ids:
            self._validate_identity(news_id, "news_id")
        if not vector:
            raise ValueError("query vector cannot be empty")
        if limit <= 0:
            raise ValueError("limit must be greater than 0")

        filters = [
            f'tenant_id == "{tenant_id}"',
            f'embedding_version == "{embedding_version}"',
        ]
        if exclude_news_ids:
            encoded = ", ".join(
                f'"{news_id}"' for news_id in sorted(exclude_news_ids)
            )
            filters.append(f"news_id not in [{encoded}]")

        rows = await asyncio.to_thread(
            self._client.search,
            collection_name=self._collections[tier],
            data=[list(vector)],
            anns_field="vector",
            filter=" and ".join(filters),
            limit=limit,
            output_fields=[field for field in self.REQUIRED_FIELDS if field != "vector"],
            search_params={
                "metric_type": self._metric_type,
                "params": self._search_params,
            },
        )
        hits = rows[0] if rows else []
        return [self._map_hit(row, tier) for row in hits]

    async def upsert(
        self,
        *,
        tier: VectorTier,
        records: Sequence[ChunkVectorRecord],
    ) -> int:
        if not records:
            return 0
        payload = [self._record_payload(record) for record in records]
        result = await asyncio.to_thread(
            self._client.upsert,
            collection_name=self._collections[tier],
            data=payload,
        )
        return int(result.get("upsert_count", len(records)))

    async def contains(
        self,
        *,
        tier: VectorTier,
        chunk_ids: Sequence[str],
    ) -> frozenset[str]:
        if not chunk_ids:
            return frozenset()
        for chunk_id in chunk_ids:
            self._validate_identity(chunk_id, "chunk_id")
        encoded = ", ".join(f'"{item}"' for item in sorted(set(chunk_ids)))
        rows = await asyncio.to_thread(
            self._client.query,
            collection_name=self._collections[tier],
            filter=f"chunk_id in [{encoded}]",
            output_fields=["chunk_id"],
            limit=len(set(chunk_ids)),
        )
        return frozenset(str(row["chunk_id"]) for row in rows)

    async def delete(
        self,
        *,
        tier: VectorTier,
        chunk_ids: Sequence[str],
    ) -> int:
        if not chunk_ids:
            return 0
        for chunk_id in chunk_ids:
            self._validate_identity(chunk_id, "chunk_id")
        encoded = ", ".join(f'"{item}"' for item in sorted(set(chunk_ids)))
        result = await asyncio.to_thread(
            self._client.delete,
            collection_name=self._collections[tier],
            filter=f"chunk_id in [{encoded}]",
        )
        return int(result.get("delete_count", 0))

    @classmethod
    def _validate_identity(cls, value: str, field: str) -> None:
        if not cls._SAFE_IDENTITY.fullmatch(value):
            raise ValueError(f"invalid {field}")

    @classmethod
    def _record_payload(cls, record: ChunkVectorRecord) -> dict[str, Any]:
        for field, value in (
            ("chunk_id", record.chunk_id),
            ("tenant_id", record.tenant_id),
            ("news_id", record.news_id),
            ("embedding_version", record.embedding_version),
        ):
            cls._validate_identity(value, field)
        if record.content_version <= 0 or record.chunk_index < 0:
            raise ValueError("invalid chunk version/index")
        if not record.vector:
            raise ValueError("chunk vector cannot be empty")
        return {
            "chunk_id": record.chunk_id,
            "tenant_id": record.tenant_id,
            "news_id": record.news_id,
            "content_version": record.content_version,
            "chunk_index": record.chunk_index,
            "embedding_version": record.embedding_version,
            "title": record.title,
            "excerpt": record.excerpt,
            "source_url": record.source_url or "",
            "publish_time_epoch": int(record.publish_time.timestamp()),
            "vector": list(record.vector),
        }

    @staticmethod
    def _map_hit(row: Mapping[str, Any], tier: VectorTier) -> VectorSearchHit:
        entity = row.get("entity") or row
        epoch = entity.get("publish_time_epoch")
        return VectorSearchHit(
            chunk_id=str(entity.get("chunk_id") or row.get("id") or ""),
            news_id=str(entity["news_id"]),
            title=str(entity.get("title") or "未命名关联报道"),
            excerpt=str(entity.get("excerpt") or ""),
            source_url=str(entity.get("source_url") or "") or None,
            publish_time=(
                datetime.fromtimestamp(int(epoch), tz=UTC)
                if epoch is not None
                else None
            ),
            raw_score=float(row.get("distance", row.get("score", 0.0))),
            tier=tier,
            content_version=int(entity.get("content_version", 1)),
            embedding_version=str(entity.get("embedding_version") or ""),
        )
