import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from app.retrieval.bge_snapshot import BGE_DIMENSIONS, _CORPUS_PATH, _MODEL_FILES, _MODEL_PATH, _verify_package
from tools.build_incremental_news_index import article_chunks, build_package, file_sha256, load_articles


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def manifest_for(base):
    (base / "SHA256SUMS").write_text("".join(
        f"{file_sha256(path)}  {path.relative_to(base).as_posix()}\n"
        for path in sorted(base.rglob("*")) if path.is_file() and path.name != "SHA256SUMS"
    ), encoding="utf-8")


@pytest.fixture
def build_inputs(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("hnswlib")
    base, source, output = tmp_path / "base", tmp_path / "source", tmp_path / "versioned-output"
    corpus = base / _CORPUS_PATH
    corpus.mkdir(parents=True)
    for name in _MODEL_FILES:
        write_json(base / _MODEL_PATH / name, {})
    write_json(base / _MODEL_PATH / "config.json", {"hidden_size": BGE_DIMENSIONS, "model_type": "bert"})
    write_json(base / _MODEL_PATH / "modules.json", [
        {"path": "", "type": "sentence_transformers.models.Transformer"},
        {"path": "1_Pooling", "type": "sentence_transformers.models.Pooling"},
        {"path": "2_Normalize", "type": "sentence_transformers.models.Normalize"},
    ])
    old_rows = [
        {"news_id": "20260903AOLD", "url": "", "publish_time": "", "chunk_index": 0,
         "text": "Old article without a structured title or date"},
        {"news_id": "20260903AOLD", "url": "", "publish_time": "", "chunk_index": 1,
         "text": "# Historical title\nOriginal body"},
    ]
    old_bytes = "".join(json.dumps(row) + "\n" for row in old_rows).encode()
    (corpus / "chunks.jsonl").write_bytes(old_bytes)
    old_vectors = np.zeros((2, BGE_DIMENSIONS), dtype=np.float32)
    old_vectors[:, 0] = 1
    np.save(corpus / "embeddings.npy", old_vectors)
    (corpus / "index.bin").write_bytes(b"original-index-verified-by-manifest")
    manifest_for(base)
    articles_dir = source / "articles"
    articles_dir.mkdir(parents=True)
    article = {"news_id": "20261014ANEW", "url": "https://news.qq.com/rain/a/20261014ANEW",
               "title": "可核验的新新闻标题", "content": "这一段来自新闻原始正文。" * 120,
               "publish_time": "2026-10-14T08:15:00+08:00", "content_version": 1}

    def add_article(value):
        article_file = articles_dir / (value["news_id"] + ".json")
        write_json(article_file, value)
        return {"news_id": value["news_id"], "relative_path": f"articles/{article_file.name}",
                "sha256": file_sha256(article_file), "publish_time": value["publish_time"],
                "document_sha256": hashlib.sha256((value["title"] + "\x1f" + value["content"]).encode()).hexdigest()}

    entry = add_article(article)
    manifest_path = source / "manifest.json"
    manifest = {"schema_version": 1, "start_date": "2026-10-07", "end_date": "2026-10-14",
                "timezone": "Asia/Shanghai", "articles": [entry], "missing_dates": ["2026-10-07"],
                "failures": [{"news_id": "unavailable"}], "counts_by_date": {"2026-10-14": 1}}
    write_json(manifest_path, manifest)
    calls = []

    class ModelStub:
        half_calls = 0

        def get_sentence_embedding_dimension(self):
            return BGE_DIMENSIONS

        def half(self):
            self.half_calls += 1
            return self

        def encode(self, texts, **kwargs):
            calls.append((texts, kwargs))
            vectors = np.zeros((len(texts), BGE_DIMENSIONS), dtype=np.float32)
            vectors[:, 1] = 1
            return vectors

    model = ModelStub()

    def factory(path, **kwargs):
        assert Path(path).is_relative_to(tmp_path)
        assert kwargs == {"device": "cpu", "local_files_only": True, "trust_remote_code": False}
        return model

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=factory))
    return SimpleNamespace(base=base, source=source, output=output, article=article, manifest=manifest,
                           manifest_path=manifest_path, articles_dir=articles_dir, add_article=add_article,
                           calls=calls, old_bytes=old_bytes, old_vectors=old_vectors, model=model)


def build(value, **kwargs):
    return build_package(articles_dir=value.articles_dir, base_package=value.base,
                         output_package=value.output, snapshot_version="news-incremental-test-v1", **kwargs)


def test_new_package_preserves_base_and_retrieves_new_vector(build_inputs):
    import hnswlib
    import numpy as np

    value = build_inputs
    before = {path.relative_to(value.base): file_sha256(path) for path in value.base.rglob("*") if path.is_file()}
    report = build(value, expected_manifest_sha256=file_sha256(value.manifest_path))
    corpus = value.output / _CORPUS_PATH
    rows = [json.loads(line) for line in (corpus / "chunks.jsonl").read_text().splitlines()]
    vectors = np.load(corpus / "embeddings.npy", allow_pickle=False)
    assert (corpus / "chunks.jsonl").read_bytes().startswith(value.old_bytes)
    assert np.array_equal(vectors[:2], value.old_vectors)
    assert all(row["url"] == value.article["url"] for row in rows[2:])
    assert all(row["publish_time"] == value.article["publish_time"] for row in rows[2:])
    assert report["added_articles"] == 1 and report["added_chunks"] == 2
    assert report["total_articles"] == 2 and report["total_chunks"] == 4
    assert report["input"]["missing_dates"] == [f"2026-10-{day:02}" for day in range(7, 14)]
    assert report["input"]["counts_by_date"] == {"2026-10-14": 1}
    assert report["input"]["collection_failure_count"] == 1
    assert _verify_package(value.output) == report["package_manifest_sha256"]
    assert before == {path.relative_to(value.base): file_sha256(path) for path in value.base.rglob("*") if path.is_file()}
    assert not value.output.with_name("." + value.output.name + ".building").exists()
    texts, options = value.calls[0]
    assert all(text.startswith("# " + value.article["title"] + "\n") for text in texts)
    assert all(not text.startswith("为这个句子") for text in texts)
    assert options["normalize_embeddings"] and options["convert_to_numpy"]
    assert value.model.max_seq_length == 512
    index = hnswlib.Index(space="ip", dim=BGE_DIMENSIONS)
    index.load_index(str(corpus / "index.bin"))
    query = np.zeros((1, BGE_DIMENSIONS), dtype=np.float32)
    query[:, 1] = 1
    labels, _ = index.knn_query(query, k=1)
    assert rows[int(labels[0][0])]["news_id"] == value.article["news_id"]


def test_existing_news_id_is_skipped_without_replacing_its_original_rows(build_inputs):
    value = build_inputs
    existing = {**value.article, "news_id": "20260903AOLD", "url": "https://news.qq.com/rain/a/20260903AOLD"}
    value.manifest["articles"].append(value.add_article(existing))
    write_json(value.manifest_path, value.manifest)
    report = build(value)
    assert report["input_articles"] == 2 and report["added_articles"] == 1
    assert report["skipped_existing_news_ids"] == ["20260903AOLD"]
    assert (value.output / _CORPUS_PATH / "chunks.jsonl").read_bytes().startswith(value.old_bytes)


@pytest.mark.parametrize("change,error", [
    ("file_hash", "file SHA-256"), ("document_hash", "document SHA-256"),
    ("url", "URL"), ("naive_date", "timezone"), ("outside_date", "window"),
    ("manifest_date", "differs"), ("duplicate", "duplicate"), ("path_escape", "escapes"),
])
def test_untrusted_manifest_and_article_fail_before_model_loading(build_inputs, change, error):
    value = build_inputs
    article = dict(value.article)
    if change == "url":
        article["url"] = "https://news.qq.com/rain/a/20261014AOTHER"
    elif change == "naive_date":
        article["publish_time"] = "2026-10-14T08:15:00"
    elif change == "outside_date":
        article["publish_time"] = "2026-10-15T08:15:00+08:00"
    if article != value.article:
        value.manifest["articles"] = [value.add_article(article)]
    entry = value.manifest["articles"][0]
    if change == "file_hash":
        entry["sha256"] = "0" * 64
    elif change == "document_hash":
        entry["document_sha256"] = "0" * 64
    elif change == "manifest_date":
        entry["publish_time"] = "2026-10-14T08:16:00+08:00"
    elif change == "duplicate":
        value.manifest["articles"].append(dict(entry))
    elif change == "path_escape":
        entry["relative_path"] = "../article.json"
    write_json(value.manifest_path, value.manifest)
    with pytest.raises(ValueError, match=error):
        build(value)
    assert value.calls == [] and not value.output.exists()


def test_manifest_hash_and_unlisted_article_are_rejected(build_inputs):
    value = build_inputs
    with pytest.raises(ValueError, match="manifest SHA-256"):
        load_articles(value.articles_dir, value.manifest_path, expected_manifest_sha256="0" * 64)
    value.add_article({**value.article, "news_id": "20261014AEXTRA", "url": "https://news.qq.com/rain/a/20261014AEXTRA"})
    with pytest.raises(ValueError, match="exactly cover"):
        build(value)
    assert value.calls == []


def test_date_window_uses_verified_publication_time_in_shanghai(build_inputs):
    value = build_inputs
    article = {**value.article, "publish_time": "2026-10-13T17:15:00Z"}
    value.manifest["articles"] = [value.add_article(article)]
    write_json(value.manifest_path, value.manifest)
    articles, _ = load_articles(value.articles_dir, value.manifest_path)
    assert articles[0]["publish_time"] == "2026-10-14T01:15:00+08:00"


def test_failed_index_build_resumes_verified_embeddings(build_inputs, monkeypatch):
    import hnswlib

    value = build_inputs
    original = hnswlib.Index

    def fail_index(**kwargs):
        raise RuntimeError("controlled index construction failure")

    monkeypatch.setattr(hnswlib, "Index", fail_index)
    with pytest.raises(RuntimeError):
        build(value)
    stage = value.output.with_name("." + value.output.name + ".building")
    assert stage.is_dir() and not value.output.exists()
    assert json.loads((stage / "build_state.json").read_text())["status"] == "failed"
    assert len(value.calls) == 1
    monkeypatch.setattr(hnswlib, "Index", original)
    report = build(value, resume=True)
    assert report["status"] == "completed" and len(value.calls) == 1
    assert not stage.exists()
    with pytest.raises(ValueError, match="already exists"):
        build(value, resume=True)


def test_partial_embedding_batches_resume_without_repeating_committed_work(build_inputs, monkeypatch):
    value = build_inputs
    original = value.model.encode
    attempts = []

    def fail_second_batch(texts, **kwargs):
        attempts.append(texts)
        if len(attempts) == 2:
            raise RuntimeError("controlled interruption during inference")
        return original(texts, **kwargs)

    monkeypatch.setattr(value.model, "encode", fail_second_batch)
    with pytest.raises(RuntimeError, match="interruption"):
        build(value, batch_size=1)
    stage = value.output.with_name("." + value.output.name + ".building")
    state = json.loads((stage / "build_state.json").read_text())
    assert state["status"] == "failed" and len(state["embedding_batches"]) == 1
    checkpoint = stage / "embedding_batches/batch-000000.npy"
    checkpoint_sha = file_sha256(checkpoint)
    assert state["embedding_batches"][0]["sha256"] == checkpoint_sha
    assert not (stage / "new_embeddings.npy").exists()
    assert len(value.calls) == 1
    monkeypatch.setattr(value.model, "encode", original)
    report = build(value, batch_size=1, resume=True)
    assert len(value.calls) == 2 and report["embedding_inference"]["checkpoint_batches"] == 2
    assert file_sha256(value.output / "embedding_batches/batch-000000.npy") == checkpoint_sha


@pytest.mark.parametrize("change", ["file", "input_hash", "batch_index"])
def test_tampered_partial_batch_is_rejected_before_more_inference(build_inputs, monkeypatch, change):
    value = build_inputs
    original = value.model.encode
    calls = []

    def fail_second_batch(texts, **kwargs):
        calls.append(texts)
        if len(calls) == 2:
            raise RuntimeError("controlled interruption")
        return original(texts, **kwargs)

    monkeypatch.setattr(value.model, "encode", fail_second_batch)
    with pytest.raises(RuntimeError):
        build(value, batch_size=1)
    stage = value.output.with_name("." + value.output.name + ".building")
    state_path = stage / "build_state.json"
    state = json.loads(state_path.read_text())
    if change == "file":
        (stage / "embedding_batches/batch-000000.npy").write_bytes(b"corrupt")
    elif change == "input_hash":
        state["embedding_batches"][0]["input_sha256"] = "0" * 64
    else:
        state["embedding_batches"][0]["batch_index"] = 17
    write_json(state_path, state)
    with pytest.raises(ValueError, match="batch checkpoint"):
        build(value, batch_size=1, resume=True)
    assert len(calls) == 2 and len(value.calls) == 1 and not value.output.exists()


def test_length_grouped_batches_restore_original_vector_row_order(build_inputs, monkeypatch):
    import numpy as np

    value = build_inputs
    rows, _ = article_chunks([{**value.article, "document_sha256": "e" * 64}], set())
    assert len(rows) == 2
    # Make the second chunk longer so inference order differs from JSONL order.
    def reordered_rows(articles, existing):
        return [dict(rows[0], text=rows[0]["text"][:100]), dict(rows[1], text=rows[1]["text"] + " longer" * 300)], []

    expected, _ = reordered_rows(None, None)
    module = sys.modules[build_package.__module__]
    monkeypatch.setattr(module, "article_chunks", reordered_rows)
    seen = []

    def identifiable_vectors(texts, **kwargs):
        seen.extend(texts)
        vectors = np.zeros((len(texts), BGE_DIMENSIONS), dtype=np.float32)
        for index, text in enumerate(texts):
            vectors[index, 2 if text == expected[0]["text"] else 3] = 1
        return vectors

    monkeypatch.setattr(value.model, "encode", identifiable_vectors)
    build(value, batch_size=1)
    vectors = np.load(value.output / _CORPUS_PATH / "embeddings.npy", allow_pickle=False)
    assert seen == [expected[1]["text"], expected[0]["text"]]
    assert vectors[2, 2] == 1 and vectors[3, 3] == 1


def test_float16_inference_is_explicit_normalized_and_bound_to_resume_identity(build_inputs, monkeypatch):
    import hnswlib
    import numpy as np

    value = build_inputs
    original_index = hnswlib.Index

    def slightly_rounded_vectors(texts, **kwargs):
        result = np.zeros((len(texts), BGE_DIMENSIONS), dtype=np.float16)
        result[:, 1] = 0.9995
        return result

    monkeypatch.setattr(value.model, "encode", slightly_rounded_vectors)
    monkeypatch.setattr(hnswlib, "Index", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("index failure")))
    with pytest.raises(RuntimeError):
        build(value, precision="float16")
    assert value.model.half_calls == 1
    with pytest.raises(ValueError, match="resume inputs differ"):
        build(value, precision="float32", resume=True)
    monkeypatch.setattr(hnswlib, "Index", original_index)
    report = build(value, precision="float16", resume=True)
    assert value.model.half_calls == 1
    assert report["embedding_inference"]["precision"] == "float16"
    assert report["embedding_inference"]["stored_vector_dtype"] == "float32"
    vectors = np.load(value.output / _CORPUS_PATH / "embeddings.npy", allow_pickle=False)
    assert vectors.dtype == np.float32 and np.allclose(np.sum(vectors * vectors, axis=1), 1.0, atol=0.000001)


def test_no_new_articles_requires_no_embedding_inference(build_inputs):
    value = build_inputs
    old_path = value.articles_dir / (value.article["news_id"] + ".json")
    old_path.unlink()
    existing = {**value.article, "news_id": "20260903AOLD", "url": "https://news.qq.com/rain/a/20260903AOLD"}
    value.manifest["articles"] = [value.add_article(existing)]
    write_json(value.manifest_path, value.manifest)
    report = build(value)
    assert value.calls == [] and report["added_chunks"] == 0
    assert report["embedding_inference"]["checkpoint_batches"] == 0


def test_changed_inputs_and_corrupted_embedding_checkpoint_cannot_resume(build_inputs, monkeypatch):
    import hnswlib

    value = build_inputs
    monkeypatch.setattr(hnswlib, "Index", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("failure")))
    with pytest.raises(RuntimeError):
        build(value)
    stage = value.output.with_name("." + value.output.name + ".building")
    (stage / "new_embeddings.npy").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checkpoint SHA-256"):
        build(value, resume=True)
    value.manifest["failures"].append({"news_id": "another_failure"})
    write_json(value.manifest_path, value.manifest)
    with pytest.raises(ValueError, match="resume inputs differ"):
        build(value, resume=True)


def test_unormalized_model_vectors_do_not_publish_a_package(build_inputs, monkeypatch):
    import numpy as np

    value = build_inputs
    monkeypatch.setattr(value.model, "encode", lambda texts, **kwargs: np.zeros((len(texts), BGE_DIMENSIONS), dtype=np.float32))
    with pytest.raises(ValueError, match="normalized"):
        build(value)
    assert not value.output.exists()


def test_body_overlap_is_deterministic_and_each_fragment_has_structured_metadata(build_inputs):
    value = build_inputs
    article = {**value.article, "content": "a" * 720 + "b" * 80 + "c" * 720,
               "document_sha256": "e" * 64}
    rows, skipped = article_chunks([article], set())
    assert skipped == [] and len(rows) == 2
    assert rows[0]["text"].split("\n\n", 1)[1][-80:] == "b" * 80
    assert rows[1]["text"].split("\n\n", 1)[1][:80] == "b" * 80
    assert rows[1]["chunk_index"] == 1 and rows[1]["document_sha256"] == "e" * 64


def test_true_short_news_is_accepted_when_manifest_binds_the_original_body(build_inputs):
    value = build_inputs
    article = {**value.article, "content": "新闻简讯：公开活动已于今日结束。"}
    value.manifest["articles"] = [value.add_article(article)]
    write_json(value.manifest_path, value.manifest)
    report = build(value)
    assert report["added_articles"] == 1 and report["added_chunks"] == 1


def test_resumed_symlink_cannot_point_a_build_write_at_the_base_package(build_inputs, monkeypatch):
    import hnswlib

    value = build_inputs
    monkeypatch.setattr(hnswlib, "Index", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("failure")))
    original_hash = file_sha256(value.base / _CORPUS_PATH / "chunks.jsonl")
    with pytest.raises(RuntimeError):
        build(value)
    stage = value.output.with_name("." + value.output.name + ".building")
    destination = stage / _CORPUS_PATH / "chunks.jsonl"
    destination.unlink()
    destination.symlink_to(value.base / _CORPUS_PATH / "chunks.jsonl")
    with pytest.raises(ValueError, match="symlinks"):
        build(value, resume=True)
    assert file_sha256(value.base / _CORPUS_PATH / "chunks.jsonl") == original_hash
