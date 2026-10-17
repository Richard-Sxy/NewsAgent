"""Milvus implementation of the tiered vector index port.

Collections are expected to share the schema documented by
``MilvusTieredVectorIndex.REQUIRED_FIELDS``.  ``pymilvus`` is imported only by
``connect`` so unit tests and non-Milvus workers do not need the dependency.
"""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from functools import partial
import re
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from app.retrieval.tiered_vector import (
    ChunkVectorRecord,
    VectorSearchHit,
    VectorTier,
)

"""这边是Milvus客户端的连接"""
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
        timeout_seconds: float = 10.0,
        consistency_level: str | None = None,
        rpc_max_concurrency: int = 32,
    ) -> None:
        if not collections:
            raise ValueError("Milvus requires at least one collection")
        if any(not name.strip() for name in collections.values()):
            raise ValueError("Milvus collection names cannot be empty")
        if consistency_level not in {None, "Strong", "Session", "Bounded", "Eventually"}:
            raise ValueError("unsupported Milvus consistency level")
        if type(rpc_max_concurrency) is not int or not 1 <= rpc_max_concurrency <= 768:
            raise ValueError("rpc_max_concurrency must be an integer in [1, 768]")
        self._client = client
        self._collections = dict(collections)
        self._metric_type = metric_type
        self._search_params = dict({"ef": 96} if search_params is None else search_params)
        self._timeout = timeout_seconds
        self._consistency_level = consistency_level
        self._lifecycle_limits = {}
        self._rpc_capacity = asyncio.Semaphore(rpc_max_concurrency)
        self._rpc_futures: set[asyncio.Future] = set()
        self._closing = False
        self._close_task: asyncio.Task | None = None

    async def validate_collections(self, *, expected_dimensions, query_max_candidates=None):
        from app.clients.enterprise.milvus_lifecycle_contract import validate_collections
        result = await validate_collections(self._client, collections=self._collections,
            expected_dimensions=expected_dimensions, metric_type=self._metric_type, timeout=self._timeout,
            search_params=self._search_params, query_max_candidates=query_max_candidates, rpc=self._rpc)
        self._lifecycle_limits = {VectorTier(tier): values["varchar_limits"] for tier, values in result.items()}
        return result

    async def _rpc(self, method, *args, **kwargs):
        """Bound submitted SDK calls, including those whose callers cancelled.

        Cancelling an asyncio waiter cannot stop a synchronous SDK thread. The
        executor Future therefore owns its slot until the real call finishes;
        cancellation while waiting for a slot submits no executor work. Calls
        still receive the configured SDK timeout. This is a per-client/process
        capacity bound, not a way to kill a driver that ignores its timeout.
        """
        if self._closing:
            raise RuntimeError("Milvus index is closing")
        await self._rpc_capacity.acquire()
        if self._closing:
            self._rpc_capacity.release()
            raise RuntimeError("Milvus index is closing")
        try:
            context = copy_context()
            future = asyncio.get_running_loop().run_in_executor(
                None, context.run, partial(method, *args, **kwargs))
        except BaseException:
            self._rpc_capacity.release()
            raise
        self._rpc_futures.add(future)
        future.add_done_callback(self._rpc_finished)
        return await asyncio.shield(future)

    def _rpc_finished(self, future):
        self._rpc_futures.discard(future)
        self._rpc_capacity.release()
        # A cancelled caller is no longer awaiting the SDK exception. Retrieve
        # it without logging provider messages or producing orphan warnings.
        if not future.cancelled():
            future.exception()

    def _read_options(self) -> dict[str, str]:
        # Legacy readers retain the collection default. Lifecycle readers opt
        # into Strong so API/Worker processes share the same visibility rule.
        return (
            {"consistency_level": self._consistency_level}
            if self._consistency_level is not None
            else {}
        )

    @property
    def tiers(self) -> tuple[VectorTier, ...]:
        return tuple(self._collections)

    @classmethod
    def connect(
        cls,
        *,
        uri: str,
        token: str | None,
        database: str,
        collections: Mapping[VectorTier, str],
        timeout_seconds: float = 10.0,
        metric_type: str = "IP",
        search_params: Mapping[str, Any] | None = None,
        consistency_level: str | None = None,
        rpc_max_concurrency: int = 32,
    ) -> "MilvusTieredVectorIndex":
        if type(rpc_max_concurrency) is not int or not 1 <= rpc_max_concurrency <= 768:
            raise ValueError("rpc_max_concurrency must be an integer in [1, 768]")
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:  # pragma：无覆盖 -依赖于环境
            raise RuntimeError(
                "pymilvus is required; install the 'milvus' extra"
            ) from exc
        client = MilvusClient(
            uri=uri,
            token=token or "",
            db_name=database,
            timeout=timeout_seconds,
        )
        return cls(client, collections=collections, metric_type=metric_type,
                   search_params=search_params, timeout_seconds=timeout_seconds,
                   consistency_level=consistency_level, rpc_max_concurrency=rpc_max_concurrency)

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close_client())
            self._close_task.add_done_callback(self._consume_close_exception)
        await asyncio.shield(self._close_task)

    @staticmethod
    def _consume_close_exception(task):
        if not task.cancelled():
            task.exception()

    async def _close_client(self):
        # No new work may be submitted after _closing is set. Do not close a
        # gRPC client while an abandoned request is still using it.
        await asyncio.gather(*(asyncio.shield(future) for future in tuple(self._rpc_futures)),
                             return_exceptions=True)
        await asyncio.to_thread(self._client.close)

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
        # 聊天迭代所有层；未配置的层被明确禁用。
        if tier not in self._collections:
            return []

        filters = [
            f'tenant_id == "{tenant_id}"',
            f'embedding_version == "{embedding_version}"',
        ]
        if exclude_news_ids:
            encoded = ", ".join(
                f'"{news_id}"' for news_id in sorted(exclude_news_ids)
            )
            filters.append(f"news_id not in [{encoded}]")

        rows = await self._rpc(
            self._client.search,
            collection_name=self._collections[tier],
            data=[list(vector)],
            anns_field="vector",
            filter=" and ".join(filters),
            limit=limit,
            timeout=self._timeout,
            output_fields=[field for field in self.REQUIRED_FIELDS if field != "vector"],
            search_params={
                "metric_type": self._metric_type,
                "params": self._search_params,
            },
            **self._read_options(),
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
        limits = self._lifecycle_limits.get(tier, {})
        if any(len(row[field].encode("utf-8")) > maximum for row in payload for field, maximum in limits.items()):
            raise ValueError("record exceeds validated Milvus VARCHAR capacity")
        result = await self._rpc(
            self._client.upsert,
            collection_name=self._collections[tier],
            data=payload,
            timeout=self._timeout,
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
        rows = await self._rpc(
            self._client.query,
            collection_name=self._collections[tier],
            filter=f"chunk_id in [{encoded}]",
            output_fields=["chunk_id"],
            limit=len(set(chunk_ids)),
            timeout=self._timeout,
            **self._read_options(),
        )
        return frozenset(str(row["chunk_id"]) for row in rows)

    async def verify_records(
        self,
        *,
        tier: VectorTier,
        records: Sequence[ChunkVectorRecord],
    ) -> bool:
        """Check the complete immutable projection metadata before activation.

        The lifecycle factory must configure Strong consistency for this query
        and subsequent searches. An ID-only presence check cannot verify a
        content revision, and a weak query cannot establish cross-process
        visibility. This check does not measure ANN recall or index readiness;
        the collection must already be loaded for lifecycle search traffic.
        """
        if not records:
            return True
        expected = {
            record.chunk_id: {
                key: value
                for key, value in self._record_payload(record).items()
                if key != "vector"
            }
            for record in records
        }
        if len(expected) != len(records):
            raise ValueError("duplicate chunk_id in verification records")
        encoded = ", ".join(f'"{item}"' for item in sorted(expected))
        rows = await self._rpc(
            self._client.query,
            collection_name=self._collections[tier],
            filter=f"chunk_id in [{encoded}]",
            output_fields=[field for field in self.REQUIRED_FIELDS if field != "vector"],
            limit=len(expected),
            timeout=self._timeout,
            **self._read_options(),
        )
        if len(rows) != len(expected):
            return False
        seen: set[str] = set()
        for row in rows:
            chunk_id = str(row.get("chunk_id", ""))
            if chunk_id in seen or chunk_id not in expected:
                return False
            seen.add(chunk_id)
            if any(row.get(field) != value for field, value in expected[chunk_id].items()):
                return False
        return seen == set(expected)

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
        result = await self._rpc(
            self._client.delete,
            collection_name=self._collections[tier],
            filter=f"chunk_id in [{encoded}]",
            timeout=self._timeout,
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
