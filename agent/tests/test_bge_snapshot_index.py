import asyncio
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.retrieval.bge_snapshot import (
    BGE_DIMENSIONS,
    BGE_QUERY_INSTRUCTION,
    BgeSnapshotSettings,
    BgeSnapshotVectorIndex,
    LocalBgeQueryEmbedder,
    _CORPUS_PATH,
    _MODEL_FILES,
    _MODEL_PATH,
)
from app.retrieval.tiered_vector import VectorTier


def unit_vector():
    return (1.0, *(0.0 for _ in range(BGE_DIMENSIONS - 1)))


class FakeModel:
    def __init__(self):
        self.calls = []
        self.dimension = BGE_DIMENSIONS
        self.active = 0
        self.max_active = 0

    def get_sentence_embedding_dimension(self):
        return self.dimension

    def encode(self, texts, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append((texts, kwargs, threading.get_ident()))
        time.sleep(0.01)
        self.active -= 1
        return [unit_vector() for _ in texts]


class FakeHnsw:
    def __init__(self, vectors):
        self.vectors = vectors
        self.labels = list(range(len(vectors)))
        self.count = len(vectors)
        self.pools = []
        self.efs = []

    def load_index(self, path):
        self.path = path

    def get_current_count(self):
        return self.count

    def get_ids_list(self):
        return self.labels

    def get_items(self, labels):
        return self.vectors[labels]

    def set_ef(self, value):
        self.efs.append(value)

    def knn_query(self, vectors, *, k):
        self.pools.append(k)
        return [self.labels[:k]], [[index / 20 for index in range(k)]]


def write_manifest(package):
    files = sorted(path for path in package.rglob("*")
                   if path.is_file() and path.name != "SHA256SUMS")
    (package / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{path.relative_to(package)}\n"
        for path in files
    ), encoding="utf-8")


@pytest.fixture
def package(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    model_dir, corpus_dir = tmp_path / _MODEL_PATH, tmp_path / _CORPUS_PATH
    corpus_dir.mkdir(parents=True)
    for name in _MODEL_FILES:
        path = model_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    (model_dir / "config.json").write_text(
        json.dumps({"hidden_size": BGE_DIMENSIONS, "model_type": "bert"}))
    (model_dir / "modules.json").write_text(json.dumps([
        {"path": "", "type": "sentence_transformers.models.Transformer"},
        {"path": "1_Pooling", "type": "sentence_transformers.models.Pooling"},
        {"path": "2_Normalize", "type": "sentence_transformers.models.Normalize"},
    ]))
    rows = [
        {"news_id": news_id, "chunk_index": label % 3, "url": "", "publish_time": "",
         "text": f"# Title {news_id}\n来源地址: https://untrusted.test/\n发布时间: 2026-01-01\n{label}"}
        for label, news_id in enumerate(["source"] * 3 + ["a"] * 5 + ["b", "c"])
    ]
    (corpus_dir / "chunks.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    vectors = np.array([unit_vector() for _ in rows], dtype=np.float32)
    np.save(corpus_dir / "embeddings.npy", vectors)
    (corpus_dir / "index.bin").write_bytes(b"fake-index")
    write_manifest(tmp_path)
    model = FakeModel()
    index = FakeHnsw(vectors)
    loads = []

    def model_factory(path, **kwargs):
        loads.append((path, kwargs))
        return model

    def index_factory(**kwargs):
        assert kwargs == {"space": "ip", "dim": BGE_DIMENSIONS}
        return index

    monkeypatch.setitem(sys.modules, "hnswlib", SimpleNamespace(Index=index_factory))
    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        SimpleNamespace(SentenceTransformer=model_factory))
    return SimpleNamespace(
        root=tmp_path, corpus=corpus_dir, model_dir=model_dir, model=model,
        index=index, loads=loads, rows=rows,
        config=BgeSnapshotSettings(tmp_path, "tenant-1", "demo-v1", chunk_pool=2, ef=2),
    )


@pytest.mark.asyncio
async def test_connect_embeds_batch_off_event_loop_with_frozen_identity(package):
    index = BgeSnapshotVectorIndex.connect(package.config)
    assert index.row_count == 10 and index.article_count == 4
    assert index.tiers == (VectorTier.HOT,)
    assert index.embedding_version == "bge-large-zh-v1.5:" + index.manifest_digest[:16]
    assert package.loads == [(str(package.model_dir), {
        "device": "cpu", "local_files_only": True, "trust_remote_code": False,
    })]
    assert package.model.max_seq_length == 512
    result = await index.query_embedder.embed_queries(["  热点  ", "关联新闻"], tenant_id="tenant-1")
    texts, kwargs, worker_thread = package.model.calls[0]
    assert texts == [BGE_QUERY_INSTRUCTION + "热点", BGE_QUERY_INSTRUCTION + "关联新闻"]
    assert kwargs["normalize_embeddings"] and kwargs["convert_to_numpy"]
    assert kwargs["batch_size"] == 8
    assert worker_thread != threading.get_ident()
    assert result == [unit_vector(), unit_vector()]
    await index.close()


@pytest.mark.asyncio
async def test_search_expands_pool_filters_source_before_article_limit(package):
    index = BgeSnapshotVectorIndex.connect(package.config)
    hits = await index.search(tier=VectorTier.HOT, tenant_id="tenant-1", vector=unit_vector(),
                              embedding_version=index.embedding_version,
                              exclude_news_ids=frozenset({"source"}), limit=2)
    assert package.index.pools == [2, 4, 8, 10]
    assert [hit.news_id for hit in hits] == ["a", "b"]
    assert hits[0].raw_score == 0.85
    assert hits[0].title == "Title a"
    assert hits[0].source_url is None and hits[0].publish_time is None
    assert hits[0].chunk_id == f"bge-snapshot:{index.manifest_digest}:3"
    assert await index.contains(tier=VectorTier.HOT, chunk_ids=[
        hits[0].chunk_id, "wrong", f"bge-snapshot:{index.manifest_digest}:03",
        f"bge-snapshot:{index.manifest_digest}:99",
    ]) == frozenset({hits[0].chunk_id})


@pytest.mark.asyncio
async def test_wrong_tenant_and_disabled_tier_cannot_read_snapshot(package):
    index = BgeSnapshotVectorIndex.connect(package.config)
    search = dict(tier=VectorTier.HOT, tenant_id="tenant-1", vector=unit_vector(),
                  embedding_version=index.embedding_version, exclude_news_ids=frozenset(), limit=1)
    assert await index.search(**dict(search, tenant_id="tenant-2")) == []
    assert await index.search(**dict(search, tier=VectorTier.COLD)) == []
    with pytest.raises(PermissionError):
        await index.query_embedder.embed_queries(["热点"], tenant_id="tenant-2")
    with pytest.raises(PermissionError):
        index.get_article("source", tenant_id="tenant-2")
    assert package.model.calls == [] and package.index.pools == []


@pytest.mark.asyncio
async def test_search_rejects_dimension_version_and_nonfinite_coordinates(package):
    index = BgeSnapshotVectorIndex.connect(package.config)
    search = dict(tier=VectorTier.HOT, tenant_id="tenant-1", vector=unit_vector(),
                  embedding_version=index.embedding_version, exclude_news_ids=frozenset(), limit=1)
    for vector in ([1.0], [0.0] * BGE_DIMENSIONS, [float("nan")] * BGE_DIMENSIONS):
        with pytest.raises(ValueError):
            await index.search(**dict(search, vector=vector))
    with pytest.raises(ValueError, match="version"):
        await index.search(**dict(search, embedding_version="unrelated-embedding"))
    assert package.index.pools == []


@pytest.mark.asyncio
async def test_embedder_serializes_models_and_validates_budget(package):
    index = BgeSnapshotVectorIndex.connect(package.config)
    await asyncio.gather(*[
        index.query_embedder.embed_queries(["热点"], tenant_id="tenant-1") for _ in range(4)
    ])
    assert package.model.max_active == 1
    for text in ("", " " * 3, "x" * 2001):
        with pytest.raises(ValueError):
            await index.query_embedder.embed_queries([text], tenant_id="tenant-1")


@pytest.mark.asyncio
async def test_write_methods_fail_and_close_is_idempotent(package):
    index = BgeSnapshotVectorIndex.connect(package.config)
    with pytest.raises(PermissionError, match="read-only"):
        await index.upsert(tier=VectorTier.HOT, records=[])
    with pytest.raises(PermissionError, match="read-only"):
        await index.delete(tier=VectorTier.HOT, chunk_ids=[])
    await index.close()
    await index.close()
    with pytest.raises(RuntimeError, match="closed"):
        index.get_article("source")
    with pytest.raises(RuntimeError, match="closed"):
        await index.query_embedder.embed_queries(["热点"], tenant_id="tenant-1")


def test_article_reconstruction_keeps_export_ids_and_does_not_infer_metadata(package):
    index = BgeSnapshotVectorIndex.connect(package.config)
    article = index.get_article("source")
    assert article["news_id"] == "source" and article["title"] == "Title source"
    assert article["publish_time"] is None and article["source_url"] is None
    assert "来源地址" in article["text"]
    assert index.get_article("unknown") is None


@pytest.mark.asyncio
async def test_missing_title_is_explicit_in_hits_and_not_inferred_in_articles(package):
    rows = package.rows
    for row in rows:
        row["text"] = "正文没有结构化标题，但提到一些实体。"
    (package.corpus / "chunks.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    write_manifest(package.root)
    index = BgeSnapshotVectorIndex.connect(package.config)
    hits = await index.search(tier=VectorTier.HOT, tenant_id="tenant-1", vector=unit_vector(),
                              embedding_version=index.embedding_version,
                              exclude_news_ids=frozenset(), limit=1)
    assert hits[0].title == "标题缺失（news_id=source）"
    assert index.get_article("source")["title"] == ""


@pytest.mark.parametrize("file", ["chunks.jsonl", "embeddings.npy", "index.bin"])
def test_manifest_blocks_modified_snapshot_before_loading_model(package, file):
    with (package.corpus / file).open("ab") as source:
        source.write(b"modified")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        BgeSnapshotVectorIndex.connect(package.config)
    assert package.loads == []


def test_manifest_blocks_model_changes_missing_entries_and_path_escape(package):
    (package.model_dir / "config.json").write_text("{}")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        BgeSnapshotVectorIndex.connect(package.config)
    write_manifest(package.root)
    manifest = package.root / "SHA256SUMS"
    manifest.write_text(manifest.read_text().replace("./data/models/bge-large-zh-v1.5/config.json",
                                                   "../config.json"))
    with pytest.raises(ValueError, match="escapes"):
        BgeSnapshotVectorIndex.connect(package.config)


@pytest.mark.parametrize("fault", ["count", "labels", "vector", "dimensions", "not-normalized"])
def test_connect_rejects_misaligned_index_or_vectors(package, fault):
    np = pytest.importorskip("numpy")
    if fault == "count":
        package.index.count -= 1
    elif fault == "labels":
        package.index.labels[-1] = 100
    elif fault == "vector":
        package.index.vectors[0, 0] = 0.0
    else:
        values = np.ones((10, 10 if fault == "dimensions" else BGE_DIMENSIONS), dtype=np.float32)
        np.save(package.corpus / "embeddings.npy", values)
        write_manifest(package.root)
    with pytest.raises(ValueError):
        BgeSnapshotVectorIndex.connect(package.config)
    assert package.loads == []


def test_connect_rejects_custom_model_modules(package):
    (package.model_dir / "modules.json").write_text(json.dumps([
        {"path": "", "type": "untrusted.module.Model"},
    ]))
    write_manifest(package.root)
    with pytest.raises(ValueError, match="approved native model"):
        BgeSnapshotVectorIndex.connect(package.config)
    assert package.loads == []


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "tenant-2"), ("url", "javascript:run()"),
    ("publish_time", "2026-01-01T00:00:00"),
])
def test_connect_rejects_conflicting_tenant_or_unreliable_structured_metadata(package, field, value):
    package.rows[0][field] = value
    (package.corpus / "chunks.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in package.rows), encoding="utf-8")
    write_manifest(package.root)
    with pytest.raises(ValueError):
        BgeSnapshotVectorIndex.connect(package.config)
    assert package.loads == []


@pytest.mark.asyncio
async def test_structured_metadata_is_kept_without_parsing_text(package):
    package.rows[0].update(url="https://verified.example/news/source",
                           publish_time="2026-01-01T00:00:00+08:00")
    (package.corpus / "chunks.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in package.rows), encoding="utf-8")
    write_manifest(package.root)
    index = BgeSnapshotVectorIndex.connect(package.config)
    hits = await index.search(tier=VectorTier.HOT, tenant_id="tenant-1", vector=unit_vector(),
                              embedding_version=index.embedding_version,
                              exclude_news_ids=frozenset(), limit=1)
    assert hits[0].source_url == "https://verified.example/news/source"
    assert hits[0].publish_time.isoformat() == "2026-01-01T00:00:00+08:00"


def test_connect_missing_dependency_fails_without_fallback(package, monkeypatch):
    monkeypatch.setitem(sys.modules, "hnswlib", None)
    with pytest.raises(RuntimeError, match="optional dependencies"):
        BgeSnapshotVectorIndex.connect(package.config)
    assert package.loads == []


@pytest.mark.asyncio
async def test_embedding_model_wrong_dimensions_and_response_count_fail(package):
    class BrokenModel:
        def encode(self, texts, **kwargs):
            return [[1.0, 0.0]]

    embedder = LocalBgeQueryEmbedder(BrokenModel(), tenant_id="tenant-1", version="model-v1")
    with pytest.raises(ValueError, match="dimensions"):
        await embedder.embed_queries(["热点"], tenant_id="tenant-1")
    with pytest.raises(ValueError, match="response count"):
        await embedder.embed_queries(["热点", "新闻"], tenant_id="tenant-1")
