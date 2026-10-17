"""Independent reference and online ANN audit semantics, without BGE inference."""

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tools.evaluate_bge_ann import (
    CORPUS, ann_neighbors, estimate, evaluate, exact_neighbors, file_sha256,
    overlap_at_k, read_rows, sample_queries, tied_chunk_overlap, verify_assets,
)


def rows(ids):
    counts, result = {}, []
    for label, news_id in enumerate(ids):
        chunk_index = counts.get(news_id, 0)
        counts[news_id] = chunk_index + 1
        result.append({"row": label, "news_id": news_id, "chunk_index": chunk_index,
                       "text_sha256": str(label)})
    return result


def test_exact_truth_excludes_all_source_chunks_and_aggregates_max_over_all_slices():
    metadata = rows(["source", "a", "a", "b", "c", "source"])
    vectors = np.asarray([[1, 0], [.9, 0], [.8, 0], [.7, 0], [.6, 0], [.99, 0]], dtype=np.float32)
    chunks, articles, timings = exact_neighbors(vectors, vectors[0], metadata, "source", np.arange(6), 3)
    assert chunks == [1, 2, 3]
    assert articles == ["a", "b", "c"]  # c is outside the top-three chunk list.
    assert timings["scan_and_rank_ms"] >= timings["ip_scan_ms"] >= 0


class FakeIndex:
    def __init__(self, count):
        self.count, self.efs, self.pools, self.threads = count, [], [], []

    def set_ef(self, ef):
        self.efs.append(ef)

    def knn_query(self, query, *, k, num_threads):
        self.pools.append(k)
        self.threads.append(num_threads)
        return np.asarray([list(range(k))]), np.asarray([[i / 100 for i in range(k)]])


def test_ann_mirrors_online_adaptive_pool_ef_source_exclusion_and_article_dedup():
    metadata = rows(["source", "a", "a", "a", "b", "c"])
    index = FakeIndex(6)
    chunks, articles, attempts = ann_neighbors(index, np.ones(2), metadata, "source",
        limit=3, requested_ef=2, chunk_pool=3, threads=1)
    assert chunks == [1, 2, 3] and articles == ["a", "b", "c"]
    assert index.pools == [3, 6] and index.efs == [3, 6] and index.threads == [1, 1]
    assert [a["chunk_pool"] for a in attempts] == [3, 6]


def test_requested_ef_below_pool_is_not_misreported_as_effective_ef():
    index = FakeIndex(5)
    _, _, attempts = ann_neighbors(index, np.ones(2), rows(["s", "a", "b", "c", "d"]),
        "s", limit=3, requested_ef=2, chunk_pool=4, threads=1)
    assert attempts[0]["effective_ef"] == 4


def test_fixed_seed_samples_distinct_articles_and_balanced_strata():
    metadata = rows([f"old{i // 2}" for i in range(20)] + [f"new{i // 2}" for i in range(20)])
    a = sample_queries(metadata, 20, 8, 42)
    assert a == sample_queries(metadata, 20, 8, 42)
    assert a != sample_queries(metadata, 20, 8, 43)
    assert [x["stratum"] for x in a] == ["base", "incremental"] * 4
    assert len({x["news_id"] for x in a}) == 8
    with pytest.raises(ValueError, match="crosses"):
        sample_queries(rows(["a", "b", "a", "c"]), 2, 2, 42)
    with pytest.raises(ValueError, match="even"):
        sample_queries(metadata, 20, 3, 42)


def test_recall_denominator_and_stratified_bootstrap_are_reproducible():
    assert overlap_at_k([1, 4], [1, 2, 3], 3) == pytest.approx(1 / 3)
    a = estimate([0, 1, .5, .5], ["base", "base", "new", "new"], seed=1, resamples=2000)
    assert a == estimate([0, 1, .5, .5], ["base", "base", "new", "new"], seed=1, resamples=2000)
    assert a["mean"] == .5 and a["ci95"][0] <= .5 <= a["ci95"][1]
    assert estimate([1, 1], ["base", "new"], seed=1, resamples=100)["ci95"] == [1, 1]


def test_tie_aware_chunk_recall_accepts_tied_rows_without_replacing_better_neighbors():
    scores = np.asarray([.9, .8, .8, .8, .1])
    assert tied_chunk_overlap([0, 3], [0, 1], scores, 2) == 1
    assert tied_chunk_overlap([2, 3], [0, 1], scores, 2) == .5


@pytest.fixture
def package(tmp_path):
    package = tmp_path / "package"
    corpus = package / CORPUS
    corpus.mkdir(parents=True)
    data = []
    for i in range(8):
        data.append({"news_id": f"article-{i}", "chunk_index": 0, "text": f"article {i}"})
    (corpus / "chunks.jsonl").write_text("".join(json.dumps(row) + "\n" for row in data))
    np.save(corpus / "embeddings.npy", np.eye(8, dtype=np.float32))
    (corpus / "index.bin").write_bytes(b"fake serialized graph")
    (package / "SHA256SUMS").write_text("".join(
        f"{file_sha256(path)}  {path.relative_to(package)}\n" for path in sorted(corpus.iterdir())))
    digest = file_sha256(package / "SHA256SUMS")
    (package / "build_report.json").write_text(json.dumps({
        "status": "completed", "snapshot_version": "test-v2", "package_manifest_sha256": digest,
        "base_manifest_sha256": "b" * 64, "base_chunks": 4, "total_chunks": 8,
        "total_articles": 8, "dimensions": 8}))
    return package, digest


def test_manifest_pin_and_used_asset_hashes_reject_mutation(package):
    path, digest = package
    assert len(verify_assets(path, digest)) == 3
    with pytest.raises(ValueError, match="pinned"):
        verify_assets(path, "a" * 64)
    (path / CORPUS / "index.bin").write_bytes(b"mutated graph")
    with pytest.raises(ValueError, match="mismatch"):
        verify_assets(path, digest)


def test_metadata_count_legacy_duplicate_row_identities_and_manifest_path_validation(package):
    path, digest = package
    metadata = path / CORPUS / "chunks.jsonl"
    assert len(read_rows(metadata, 8)) == 8
    with pytest.raises(ValueError, match="count"):
        read_rows(metadata, 9)
    with metadata.open("a") as output:
        output.write(metadata.read_text().splitlines()[0] + "\n")
    # Legacy duplicate identities remain distinct row-based online chunk_ids.
    assert len(read_rows(metadata, 9)) == 9
    manifest = path / "SHA256SUMS"
    manifest.write_text("0" * 64 + "  ../outside\n")
    with pytest.raises(ValueError, match="unsafe"):
        verify_assets(path, file_sha256(manifest))


def test_end_to_end_binds_identity_vectors_sampling_and_report_without_model(package, monkeypatch):
    path, digest = package

    class ExactIndex:
        def __init__(self, *, space, dim):
            assert space == "ip" and dim == 8

        def load_index(self, filename):
            self.vectors = np.eye(8, dtype=np.float32)

        def set_num_threads(self, threads):
            assert threads == 1

        def get_current_count(self):
            return 8

        def get_ids_list(self):
            return list(range(8))

        def get_items(self, labels):
            return self.vectors[labels]

        def set_ef(self, ef):
            pass

        def knn_query(self, query, *, k, num_threads):
            scores = self.vectors @ query[0]
            labels = np.lexsort((np.arange(8), -scores))[:k]
            return labels[None, :], (1 - scores[labels])[None, :]

    monkeypatch.setitem(sys.modules, "hnswlib", SimpleNamespace(Index=ExactIndex))
    import tools.evaluate_bge_ann as module
    real_version = module.importlib.metadata.version
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: "fake-test" if name == "hnswlib" else real_version(name))
    kwargs = dict(package=path, expected_manifest_sha256=digest, snapshot_version="test-v2",
                  expected_rows=8, expected_articles=8, dimensions=8, base_rows=4,
                  expected_base_manifest_sha256="b" * 64, sample_size=4, ks=(1, 2),
                  efs=(2, 8), chunk_pool=4, threads=1, warmup_queries=1, bootstrap_resamples=100)
    result = evaluate(**kwargs)
    assert result["status"] == "completed" and result["package_manifest_sha256"] == digest
    assert len(result["queries"]) == 4 and len(result["observations"]) == 16
    assert all(x["article_ann_at_k"] == 1 and x["chunk_ann_at_k"] == 1 for x in result["observations"])
    assert result["parameters"]["threads"] == 1
    assert all(pool["num_threads"] == 1 for pool in result["runtime"]["threadpools_during_run"])
    assert file_sha256(path / "SHA256SUMS") == digest
    with pytest.raises(ValueError, match="build report"):
        evaluate(**{**kwargs, "snapshot_version": "wrong-v3"})
