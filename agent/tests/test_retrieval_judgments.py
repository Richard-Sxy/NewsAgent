"""The annotation starter cannot accidentally supply relevance truth or rank cues."""

import hashlib
import json
from pathlib import Path

import pytest

from tools import prepare_retrieval_judgments as prep


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "originals"
    articles, entries, chunks = {}, [], []
    for day in ("2026-10-07", "2026-10-08"):
        for number in range(14):
            nid = day.replace("-", "") + f"A{number:03d}"
            category = "auto" if number % 2 else "edu"
            title = ("汽车产业" if category == "auto" else "教育发展") + nid
            body = ("完整证据，应当保留超过六百字的原始正文。" * 50) + nid
            digest = hashlib.sha256(f"{title}\x1f{body}".encode()).hexdigest()
            article = {"news_id": nid, "title": title, "content": body,
                       "publish_time": day + "T10:00:00+08:00", "category": category,
                       "url": "https://news.qq.com/rain/a/" + nid, "content_version": 1}
            path = root / "articles" / (nid + ".json")
            write_json(path, article)
            entries.append({"news_id": nid, "relative_path": "articles/" + path.name,
                            "sha256": prep.file_sha256(path), "document_sha256": digest,
                            "publish_time": article["publish_time"]})
            articles[nid] = {**article, "document_sha256": digest}
            chunks.append({"news_id": nid, "chunk_index": 0, "text": title + "\n" + body})
    manifest_path = root / "manifest.json"
    write_json(manifest_path, {"schema_version": 1, "timezone": "Asia/Shanghai",
                              "start_date": "2026-10-07", "end_date": "2026-10-08", "articles": entries})
    package = tmp_path / "index"
    chunks_path = package / prep.CORPUS / "chunks.jsonl"
    chunks_path.parent.mkdir(parents=True)
    chunks_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in chunks) + "\n")
    (package / "SHA256SUMS").write_text(f"{prep.file_sha256(chunks_path)}  {prep.CORPUS}/chunks.jsonl\n")
    selected = prep.select_sources(articles, per_day=2, seed=42)
    drafts = tmp_path / "drafts.json"
    write_json(drafts, {"question_search": [{"seed_news_id": nid, "query": f"有关{nid}的背景资料中有哪些具体变化？"}
                                            for nid in selected],
                        "no_answer_candidate": ["是否有关于海底火星城的新消息？"]})
    return {"package": package, "index_manifest_sha256": prep.file_sha256(package / "SHA256SUMS"),
            "articles_manifest": manifest_path, "articles_manifest_sha256": prep.file_sha256(manifest_path),
            "drafts_path": drafts, "snapshot_version": "draft-test-v1", "seed": 42,
            "pool_size": 12, "dense": False}, articles


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_starter_has_no_truth_and_blind_pairs_have_no_retrieval_cues(corpus, tmp_path):
    settings, originals = corpus
    output = tmp_path / "annotation"
    result = prep.prepare(**settings, output_dir=output)
    queries = rows(output / "queries.jsonl")
    pairs = rows(output / "annotation_tasks.jsonl")
    evidence = {row["news_id"]: row for row in rows(output / "evidence.jsonl")}
    assert result["query_counts"] == {"related_news": 4, "question_search": 4, "no_answer_candidate": 1}
    assert result["annotation_pairs"] == 108
    assert result["annotation_pool_scope"] == "source_manifest_only"
    assert result["source_verified_full_body"] == result["index_total"] == 28
    assert result["semantic_quality_score_computed"] is False
    assert result["query_dense_embeddings_computed"] is False
    assert all(row["split"] is row["event_group_id"] is row["expected_empty"] is None for row in queries)
    assert all(row["grade"] is row["reviewer_id"] is None for row in pairs)
    assert all(row["as_of"] == "2026-10-08T23:59:59+08:00" for row in pairs)
    assert all(row["news_id"] != row["source_news_id"] for row in pairs if row["task"] == "related_news")
    assert not any({"score", "rank", "proposals", "seed_news_id", "sampling_category"} & set(row) for row in pairs)
    assert len({row["pair_id"] for row in pairs}) == len(pairs)
    for row in pairs:
        assert row["evidence_sha256"] == evidence[row["news_id"]]["document_sha256"]
        assert evidence[row["news_id"]]["content"] == originals[row["news_id"]]["content"]
        assert len(evidence[row["news_id"]]["content"]) > 600
    provenance = rows(output / "candidate_provenance.jsonl")
    assert all(row["query_dense_embedding_computed"] is False for row in provenance)
    assert all(row["source_seed_not_a_positive_label"] for row in provenance
               if row["query_id"].startswith("question_search-"))
    assert any(proposal["method"] == "source_seed_not_a_positive_label" for row in provenance
               for candidate in row["candidates"] for proposal in candidate["proposals"])
    for name, digest in result["output_sha256"].items():
        assert prep.file_sha256(output / name) == digest
    with pytest.raises(ValueError, match="already exists"):
        prep.prepare(**settings, output_dir=output)


def test_reproducible_blind_content_and_dates_are_stratified(corpus, tmp_path):
    settings, articles = corpus
    sources = prep.select_sources(articles, per_day=2, seed=42)
    assert len(sources) == len(set(sources)) == 4
    assert [articles[nid]["publish_time"][:10] for nid in sources] == ["2026-10-07"] * 2 + ["2026-10-08"] * 2
    for offset in (0, 2):
        assert len({prep.draft_topic(articles[nid]) for nid in sources[offset:offset + 2]}) == 2
    first = prep.prepare(**settings, output_dir=tmp_path / "first")
    second = prep.prepare(**settings, output_dir=tmp_path / "second")
    assert first["output_sha256"] == second["output_sha256"]


def test_pin_mismatch_fails_before_output(corpus, tmp_path):
    settings, _ = corpus
    settings["index_manifest_sha256"] = "0" * 64
    output = tmp_path / "failed"
    with pytest.raises(ValueError, match="SHA-256"):
        prep.prepare(**settings, output_dir=output)
    assert not output.exists()


def test_original_evidence_tampering_is_rejected(corpus, tmp_path):
    settings, _ = corpus
    path = next((settings["articles_manifest"].parent / "articles").glob("*.json"))
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="SHA-256"):
        prep.prepare(**settings, output_dir=tmp_path / "failed")


def test_title_copy_and_missing_question_drafts_are_rejected(corpus):
    settings, articles = corpus
    ids = prep.select_sources(articles, per_day=2, seed=42)
    drafts = json.loads(settings["drafts_path"].read_text())
    drafts["question_search"][0]["query"] = articles[ids[0]]["title"]
    with pytest.raises(ValueError, match="copy"):
        prep.make_queries(ids, drafts, articles, "2026-10-08T23:59:59+08:00")
    drafts["question_search"].pop()
    with pytest.raises(ValueError, match="cover"):
        prep.make_queries(ids, drafts, articles, "2026-10-08T23:59:59+08:00")


def test_related_pool_excludes_all_source_versions_and_exact_document_copies():
    articles = {"source": {"document_sha256": "same"}, "copy": {"document_sha256": "same"},
                **{f"n{i}": {"document_sha256": str(i)} for i in range(12)}}
    proposals = {"dense": [(nid, 0.9) for nid in articles]}
    selected = prep.merge_pool(proposals, query_id="q", source_id="source", articles=articles,
                               pool_size=12, seed=42)
    assert "source" not in selected and "copy" not in selected
    assert len(selected) == 12


def test_stored_source_dense_proposals_are_article_max_and_not_self():
    np = pytest.importorskip("numpy")
    vectors = np.asarray([[1., 0.], [1., 0.], [.6, .8], [.8, .6], [0., 1.]], dtype="float32")
    candidates = prep.dense_source_candidates(vectors, ["s", "s", "a", "a", "b"], 0, "s",
                                               {"s": {}, "a": {}, "b": {}}, 10)
    assert [nid for nid, _ in candidates] == ["a", "b"]
    assert candidates[0][1] == pytest.approx(.8)


def test_duplicate_legacy_rows_are_audited_but_eligible_duplicates_fail(tmp_path):
    path = tmp_path / "chunks.jsonl"
    data = [{"news_id": nid, "chunk_index": 0, "text": "source text"}
            for nid in ("legacy", "legacy", "current")]
    path.write_text("\n".join(json.dumps(row) for row in data) + "\n")
    row_ids, first_rows, duplicates = prep.read_index_rows(path, {"current"})
    assert row_ids == ["legacy", "legacy", "current"]
    assert first_rows == {"current": 2} and duplicates == 1
    with pytest.raises(ValueError, match="duplicated eligible"):
        prep.read_index_rows(path, {"legacy", "current"})
