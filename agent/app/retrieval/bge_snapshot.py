"""Native, read-only BGE query embedding and HNSW snapshot retrieval.

The export has no tenant ACL or authoritative article metadata. An explicitly
configured tenant owns access to this immutable local snapshot; article dates
and URLs are accepted only from structured export fields, never from its text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from app.retrieval.tiered_vector import ChunkVectorRecord, VectorSearchHit, VectorTier


BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
BGE_DIMENSIONS = 1024
_CORPUS_PATH = Path("data/experiments/offline_recall_fg")
_MODEL_PATH = Path("data/models/bge-large-zh-v1.5")
_MODEL_FILES = (
    "1_Pooling/config.json", "config.json", "config_sentence_transformers.json",
    "configuration.json", "modules.json", "pytorch_model.bin",
    "sentence_bert_config.json", "special_tokens_map.json", "tokenizer.json",
    "tokenizer_config.json", "vocab.txt",
)


@dataclass(frozen=True, slots=True)
class BgeSnapshotSettings:
    package_dir: str | Path
    tenant_id: str
    snapshot_version: str
    device: str = "cpu"
    chunk_pool: int = 200
    ef: int = 256
    max_query_chars: int = 2000
    model_batch_size: int = 8

    def __post_init__(self) -> None:
        if not str(self.package_dir).strip():
            raise ValueError("BGE snapshot package_dir is required")
        if not self.tenant_id.strip() or not self.snapshot_version.strip():
            raise ValueError("BGE snapshot tenant_id and snapshot_version are required")
        if not self.device.strip():
            raise ValueError("BGE snapshot device is required")
        for name in ("chunk_pool", "ef", "max_query_chars", "model_batch_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"BGE snapshot {name} must be a positive integer")


def _checked_vector(vector: Sequence[float]) -> tuple[float, ...]:
    if len(vector) != BGE_DIMENSIONS:
        raise ValueError("BGE query/index embedding dimensions must equal 1024")
    try:
        result = tuple(float(value) for value in vector)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("BGE query vector contains invalid coordinates") from exc
    if any(not math.isfinite(value) for value in result):
        raise ValueError("BGE query vector contains invalid coordinates")
    if not math.isclose(sum(value * value for value in result), 1.0, abs_tol=0.001):
        raise ValueError("BGE query vector must be normalized")
    return result


class LocalBgeQueryEmbedder:
    """Same query prefix, dimensions and normalization as the frozen export.

    Heavy inference runs in a worker thread. A model lock serializes access even
    when a cancelled await leaves its worker running until inference completes.
    """

    def __init__(self, model: Any, *, tenant_id: str, version: str,
                 max_query_chars: int = 2000, batch_size: int = 8) -> None:
        if not tenant_id.strip() or not version.strip():
            raise ValueError("BGE query embedding identity is incomplete")
        if max_query_chars <= 0 or batch_size <= 0:
            raise ValueError("BGE query embedding budgets must be positive")
        self._model = model
        self._tenant_id = tenant_id
        self.version = version
        self._max_query_chars = max_query_chars
        self._batch_size = batch_size
        self._lock = threading.Lock()

    async def embed_queries(self, texts: Sequence[str], *,
                            tenant_id: str) -> list[tuple[float, ...]]:
        if tenant_id != self._tenant_id:
            raise PermissionError("BGE snapshot is not available to this tenant")
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("BGE query must not be blank")
        if any(len(text) > self._max_query_chars for text in texts):
            raise ValueError("BGE query exceeds configured character budget")
        return await asyncio.to_thread(self._encode, tuple(text.strip() for text in texts))

    def _encode(self, texts: tuple[str, ...]) -> list[tuple[float, ...]]:
        with self._lock:
            if self._model is None:
                raise RuntimeError("BGE snapshot query embedder is closed")
            if not texts:
                return []
            vectors = self._model.encode(
                [BGE_QUERY_INSTRUCTION + text for text in texts],
                batch_size=self._batch_size, normalize_embeddings=True,
                convert_to_numpy=True, show_progress_bar=False,
            )
            if len(vectors) != len(texts):
                raise ValueError("BGE query embedding response count differs from input")
            return [_checked_vector(vector) for vector in vectors]

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self._lock:
            self._model = None


def _verify_package(package_dir: Path) -> str:
    manifest = package_dir / "SHA256SUMS"
    if not manifest.is_file() or manifest.stat().st_size > 1_048_576:
        raise ValueError("BGE snapshot SHA256SUMS missing or exceeds 1 MiB")
    content = manifest.read_bytes()
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ValueError("BGE snapshot SHA256SUMS is not UTF-8") from exc
    entries: dict[Path, str] = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
        if match is None:
            raise ValueError("BGE snapshot SHA256SUMS contains an invalid entry")
        path = Path(match.group(2))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("BGE snapshot manifest path escapes package_dir")
        if path in entries:
            raise ValueError("BGE snapshot SHA256SUMS contains duplicate paths")
        entries[path] = match.group(1).lower()
    required = {
        *(_CORPUS_PATH / name for name in ("chunks.jsonl", "embeddings.npy", "index.bin")),
        *(_MODEL_PATH / name for name in _MODEL_FILES),
    }
    model_dir = package_dir / _MODEL_PATH
    if not model_dir.is_dir():
        raise ValueError("BGE snapshot model directory is missing")
    # Any extra files the loader could read also need manifest coverage. Bytecode
    # and custom model code are intentionally not part of this native adapter.
    required.update(path.relative_to(package_dir) for path in model_dir.rglob("*")
                    if path.is_file())
    for relative in sorted(required):
        if relative not in entries:
            raise ValueError(f"BGE snapshot manifest does not cover {relative}")
        path = package_dir / relative
        if not path.is_file() or not path.resolve().is_relative_to(package_dir):
            raise ValueError(f"BGE snapshot file missing or escapes package_dir: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1_048_576), b""):
                digest.update(block)
        if digest.hexdigest() != entries[relative]:
            raise ValueError(f"BGE snapshot SHA-256 mismatch: {relative}")
    return hashlib.sha256(content).hexdigest()


def _validate_model_config(model_dir: Path) -> None:
    config = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
    if config.get("hidden_size") != BGE_DIMENSIONS or config.get("model_type") != "bert":
        raise ValueError("BGE snapshot model must be the 1024-dimensional BERT model")
    modules = json.loads((model_dir / "modules.json").read_text(encoding="utf-8"))
    expected = (
        ("", "sentence_transformers.models.Transformer"),
        ("1_Pooling", "sentence_transformers.models.Pooling"),
        ("2_Normalize", "sentence_transformers.models.Normalize"),
    )
    if not isinstance(modules, list) or len(modules) != len(expected):
        raise ValueError("BGE snapshot model modules differ from the approved native model")
    if any(not isinstance(module, dict) or
           (module.get("path"), module.get("type")) != wanted
           for module, wanted in zip(modules, expected, strict=True)):
        raise ValueError("BGE snapshot model modules differ from the approved native model")


@dataclass(frozen=True, slots=True)
class _SnapshotChunk:
    news_id: str
    chunk_index: int
    text: str
    title: str
    source_url: str | None
    publish_time: datetime | None


def _read_chunks(path: Path, *, tenant_id: str) -> tuple[_SnapshotChunk, ...]:
    chunks = []
    with path.open(encoding="utf-8") as source:
        for row_number, line in enumerate(source):
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"BGE snapshot chunk {row_number} is not an object")
            news_id, text, chunk_index = (row.get("news_id"), row.get("text"),
                                          row.get("chunk_index"))
            if (not isinstance(news_id, str) or not news_id.strip() or
                    not isinstance(text, str) or not text.strip() or
                    isinstance(chunk_index, bool) or not isinstance(chunk_index, int) or
                    chunk_index < 0):
                raise ValueError(f"BGE snapshot chunk {row_number} has invalid content identity")
            if row.get("tenant_id", tenant_id) != tenant_id:
                raise ValueError("BGE snapshot chunk tenant differs from configured tenant")
            url, published = row.get("url"), row.get("publish_time")
            if url not in (None, ""):
                if not isinstance(url, str):
                    raise ValueError("BGE snapshot structured url must be a string")
                parsed = urlsplit(url)
                if (parsed.scheme not in {"https", "http"} or not parsed.hostname or
                        parsed.username or parsed.password or any(char.isspace() for char in url)):
                    raise ValueError("BGE snapshot structured url is invalid")
            timestamp = None
            if published not in (None, ""):
                if not isinstance(published, str):
                    raise ValueError("BGE snapshot structured publish_time must be a string")
                timestamp = datetime.fromisoformat(published)
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("BGE snapshot structured publish_time requires a timezone")
            first_line = text.splitlines()[0]
            chunks.append(_SnapshotChunk(news_id, chunk_index, text,
                         first_line[2:].strip() if first_line.startswith("# ") else "",
                         url or None, timestamp))
    if not chunks:
        raise ValueError("BGE snapshot contains no chunks")
    return tuple(chunks)


class BgeSnapshotVectorIndex:
    """One immutable HOT tier; it denotes configured routing, not article heat."""

    def __init__(self, index: Any, chunks: Sequence[_SnapshotChunk], *,
                 tenant_id: str, snapshot_version: str, manifest_digest: str,
                 query_embedder: LocalBgeQueryEmbedder, chunk_pool: int = 200,
                 ef: int = 256) -> None:
        self._index = index
        self._chunks = tuple(chunks)
        self.tenant_id = tenant_id
        self.snapshot_version = snapshot_version
        self.manifest_digest = manifest_digest
        self.embedding_version = query_embedder.version
        self.query_embedder = query_embedder
        self._chunk_pool, self._ef = chunk_pool, ef
        self._lock = threading.RLock()
        self._article_rows: dict[str, list[int]] = {}
        self._titles: dict[str, str] = {}
        for label, chunk in enumerate(self._chunks):
            self._article_rows.setdefault(chunk.news_id, []).append(label)
            if chunk.title:
                self._titles.setdefault(chunk.news_id, chunk.title)
        self.row_count = len(self._chunks)
        self.article_count = len(self._article_rows)

    @property
    def tiers(self) -> tuple[VectorTier, ...]:
        return (VectorTier.HOT,)

    @classmethod
    def connect(cls, config: Any) -> BgeSnapshotVectorIndex:
        settings = BgeSnapshotSettings(**{
            name: getattr(config, name, default)
            for name, default in (
                ("package_dir", ""), ("tenant_id", ""), ("snapshot_version", ""),
                ("device", "cpu"), ("chunk_pool", 200), ("ef", 256),
                ("max_query_chars", 2000), ("model_batch_size", 8),
            )
        })
        package_dir = Path(settings.package_dir).resolve()
        manifest_digest = _verify_package(package_dir)
        model_dir, corpus_dir = package_dir / _MODEL_PATH, package_dir / _CORPUS_PATH
        _validate_model_config(model_dir)
        chunks = _read_chunks(corpus_dir / "chunks.jsonl", tenant_id=settings.tenant_id)
        try:
            import hnswlib
            import numpy as np
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("BGE snapshot requires the 'bge-snapshot' optional dependencies") from exc
        vectors = np.load(corpus_dir / "embeddings.npy", mmap_mode="r", allow_pickle=False)
        try:
            if vectors.shape != (len(chunks), BGE_DIMENSIONS) or vectors.dtype != np.float32:
                raise ValueError("BGE snapshot vectors must be float32 with matching rows and 1024 dimensions")
            index = hnswlib.Index(space="ip", dim=BGE_DIMENSIONS)
            index.load_index(str(corpus_dir / "index.bin"))
            if index.get_current_count() != len(chunks):
                raise ValueError("BGE snapshot index and chunk row counts differ")
            if sorted(index.get_ids_list()) != list(range(len(chunks))):
                raise ValueError("BGE snapshot HNSW labels must equal chunk row numbers")
            for offset in range(0, len(chunks), 1024):
                block = vectors[offset:offset + 1024]
                if not np.isfinite(block).all() or not np.allclose(
                        np.linalg.norm(block, axis=1), 1.0, atol=0.001):
                    raise ValueError("BGE snapshot vectors must be finite and normalized")
                indexed = index.get_items(list(range(offset, offset + len(block))))
                if not np.allclose(indexed, block, atol=0.000001):
                    raise ValueError("BGE snapshot HNSW vectors differ from their labeled rows")
            index.set_ef(max(settings.ef, settings.chunk_pool))
        finally:
            mapped = getattr(vectors, "_mmap", None)
            if mapped is not None:
                mapped.close()
        model = SentenceTransformer(str(model_dir), device=settings.device,
                                    local_files_only=True, trust_remote_code=False)
        model.max_seq_length = 512
        dimension_reader = getattr(model, "get_embedding_dimension", None)
        if dimension_reader is None:
            dimension_reader = model.get_sentence_embedding_dimension
        if dimension_reader() != BGE_DIMENSIONS:
            raise ValueError("BGE snapshot model embedding dimensions differ from the index")
        embedder = LocalBgeQueryEmbedder(
            model, tenant_id=settings.tenant_id,
            version=f"bge-large-zh-v1.5:{manifest_digest[:16]}",
            max_query_chars=settings.max_query_chars, batch_size=settings.model_batch_size,
        )
        return cls(index, chunks, tenant_id=settings.tenant_id,
                   snapshot_version=settings.snapshot_version, manifest_digest=manifest_digest,
                   query_embedder=embedder, chunk_pool=settings.chunk_pool, ef=settings.ef)

    def _ensure_open(self) -> None:
        if self._index is None:
            raise RuntimeError("BGE snapshot index is closed")

    def _chunk_id(self, label: int) -> str:
        return f"bge-snapshot:{self.manifest_digest}:{label}"

    async def search(self, *, tier: VectorTier, tenant_id: str,
                     vector: Sequence[float], embedding_version: str,
                     exclude_news_ids: frozenset[str], limit: int) -> list[VectorSearchHit]:
        self._ensure_open()
        if not tenant_id.strip() or limit <= 0:
            raise ValueError("BGE vector search context is invalid")
        if tenant_id != self.tenant_id or tier != VectorTier.HOT:
            return []
        if embedding_version != self.embedding_version:
            raise ValueError("BGE query embedding version differs from the frozen snapshot")
        checked = _checked_vector(vector)
        return await asyncio.to_thread(self._search, checked, exclude_news_ids, limit)

    def _search(self, vector: tuple[float, ...], excluded: frozenset[str],
                limit: int) -> list[VectorSearchHit]:
        with self._lock:
            self._ensure_open()
            pool = min(max(self._chunk_pool, limit), self.row_count)
            while True:
                self._index.set_ef(max(self._ef, pool))
                labels, distances = self._index.knn_query([vector], k=pool)
                hits: dict[str, VectorSearchHit] = {}
                for label, distance in zip(labels[0], distances[0], strict=True):
                    label, score = int(label), 1.0 - float(distance)
                    if not 0 <= label < self.row_count or not math.isfinite(score):
                        raise ValueError("BGE snapshot returned an invalid index hit")
                    chunk = self._chunks[label]
                    if chunk.news_id in excluded:
                        continue
                    candidate = VectorSearchHit(
                        chunk_id=self._chunk_id(label), news_id=chunk.news_id,
                        title=self._titles.get(chunk.news_id) or
                              f"标题缺失（news_id={chunk.news_id}）", excerpt=chunk.text,
                        source_url=chunk.source_url, publish_time=chunk.publish_time,
                        raw_score=score, tier=VectorTier.HOT, content_version=1,
                        embedding_version=self.embedding_version,
                    )
                    previous = hits.get(chunk.news_id)
                    if previous is None or candidate.raw_score > previous.raw_score:
                        hits[chunk.news_id] = candidate
                ranked = sorted(hits.values(), key=lambda hit: (-hit.raw_score, hit.chunk_id))
                if len(ranked) >= limit or pool == self.row_count:
                    return ranked[:limit]
                pool = min(pool * 2, self.row_count)

    def get_article(self, news_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        """CLI helper to reconstruct a source article; not a canonical news store."""
        with self._lock:
            self._ensure_open()
            if tenant_id is not None and tenant_id != self.tenant_id:
                raise PermissionError("BGE snapshot is not available to this tenant")
            labels = self._article_rows.get(news_id)
            if labels is None:
                return None
            ordered = sorted(labels, key=lambda label: (self._chunks[label].chunk_index, label))
            rows = [self._chunks[label] for label in ordered]
            text = "\n\n".join(dict.fromkeys(row.text for row in rows))
            first = rows[0]
            return {
                "news_id": news_id, "title": self._titles.get(news_id, ""),
                "summary": first.text, "text": text,
                "publish_time": first.publish_time.isoformat() if first.publish_time else None,
                "source_url": first.source_url,
            }

    async def contains(self, *, tier: VectorTier,
                       chunk_ids: Sequence[str]) -> frozenset[str]:
        with self._lock:
            self._ensure_open()
            if tier != VectorTier.HOT:
                return frozenset()
            prefix = f"bge-snapshot:{self.manifest_digest}:"
            return frozenset(chunk_id for chunk_id in chunk_ids
                             if chunk_id.startswith(prefix) and
                             chunk_id[len(prefix):].isdigit() and
                             chunk_id == self._chunk_id(int(chunk_id[len(prefix):])) and
                             int(chunk_id[len(prefix):]) < self.row_count)

    async def upsert(self, *, tier: VectorTier,
                     records: Sequence[ChunkVectorRecord]) -> int:
        raise PermissionError("BGE snapshot is read-only; rebuild a new version to update it")

    async def delete(self, *, tier: VectorTier, chunk_ids: Sequence[str]) -> int:
        raise PermissionError("BGE snapshot is read-only; rebuild a new version to update it")

    async def close(self) -> None:
        await asyncio.to_thread(self._close_index)
        await self.query_embedder.close()

    def _close_index(self) -> None:
        with self._lock:
            self._index = None
            self._chunks = ()
            self._article_rows.clear()
            self._titles.clear()
