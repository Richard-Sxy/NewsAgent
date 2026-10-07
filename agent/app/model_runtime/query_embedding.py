"""
把新闻检索问题转化为向量，供关联检索使用。
"""

from __future__ import annotations

from hashlib import sha256
from typing import Sequence

from app.model_runtime.core import EmbeddingPort, EmbeddingRequest


class ModelQueryEmbedder:
    def __init__(self, embedding: EmbeddingPort, *, version: str) -> None:
        if not version.strip():
            raise ValueError("embedding version is required")
        self._embedding = embedding
        self.version = version

    async def embed_queries(
        self,
        texts: Sequence[str],
        *,
        tenant_id: str,
    ) -> list[tuple[float, ...]]:
        if not texts:
            return []
        digest = sha256("\x1f".join(texts).encode("utf-8")).hexdigest()
        result = await self._embedding.embed(
            EmbeddingRequest(
                tenant_id=tenant_id,
                trace_id=f"knowledge-query-{digest}",
                model_route=self.version,
                texts=tuple(texts),
            )
        )
        if result.model_version != self.version:
            raise ValueError("query embedding version drifted")
        if len(result.vectors) != len(texts):
            raise ValueError("query embedding response count differs from input")
        return list(result.vectors)
