"""
本地哈希向量实现，用于隔离测试。
"""

from __future__ import annotations

import math
from hashlib import sha256

from app.model_runtime.core import EmbeddingRequest, EmbeddingResult


class LocalHashEmbedding:
    def __init__(self, *, model_routes: tuple[str, ...], dimensions: int = 32) -> None:
        if not model_routes or dimensions < 4:
            raise ValueError("local embedding routes and dimensions are required")
        self._routes = frozenset(model_routes)
        self._dimensions = dimensions

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        if request.model_route not in self._routes:
            raise ValueError("unapproved local embedding model route")
        vectors = tuple(self._vectorize(text) for text in request.texts)
        return EmbeddingResult(vectors=vectors, model_version=request.model_route)

    def _vectorize(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self._dimensions
        normalized = text.casefold().strip()
        grams = [*normalized, *(normalized[index:index + 2] for index in range(len(normalized) - 1))]
        for gram in grams:
            digest = sha256(gram.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self._dimensions
            vector[bucket] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            vector[0] = 1.0
            norm = 1.0
        return tuple(value / norm for value in vector)
