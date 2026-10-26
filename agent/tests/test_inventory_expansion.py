"""Offline candidate integrity, leakage boundaries and no-gold contracts."""
from copy import deepcopy
import json

import pytest

from app.modules.news_storage.model import build_package, canonical_json, normalize_record
from app.modules.news_storage.package_io import save_package
from evaluation.inventory_expansion import (
    build, file_hash, load_source, publish, render_preview, strict_json,
    title_hits, validate_dataset, verify,
)
from tools.score_retrieval_judgments import EvaluationBlocked, score


def article(nid, title, body="", **changes):
    raw = {
        "news_id": nid, "title": title,
        "body": (nid + "正文证据。") * 40 if body == "" else body,
        "url": "https://example.org/" + nid,
        "publish_time": "2026-10-10T00:00:00+00:00",
        "content_type": "article", "content_state": "body_available",
        "fragments": [], "quality_flags": [],
    }
    raw.update(changes)
    return {**normalize_record(raw), "selection_quality_flags": [],
            "release_id": "news-fixture", "tenant_id": "test",
            "tags": {"topic_ids": []}, "provenance": []}


@pytest.fixture
def inputs():
    rows = [article(f"20261010A000{i:03d}", title) for i, title in enumerate((
        "AI 火山地质观测方案", "AI 海洋船舶能源研究", "AI 工业生产质量分析",
        "AI 城市道路出行预测", "AI 南极冰川变化发现", "AI 空间飞行材料实验",
        "AI 林业保护设备开发", "AI 平台算法开源发布",
    ))]
    policy = {
        "version": "test-v1", "as_of": "2026-10-15T00:00:00+08:00",
        "min_source_body_chars": 300, "min_candidate_body_chars": 100,
        "sources_per_topic": 2, "candidates_per_query": 5,
        "quality_cases_per_stratum": 1, "seed": 17, "near_title_jaccard": .65,
        "topics": [{"id": "ai", "name": "AI", "title_terms": ["AI"]}],
    }
    return rows, policy


def test_deterministic_drafts_are_not_gold_or_scoring_input(inputs):
    rows, policy = inputs
    dataset, report = build(rows, policy, set())
    assert (dataset, report) == build(list(reversed(rows)), policy, set())
    assert report["related_news_queries"] == 2
    assert report["review_pairs"] == 10
    assert report["training_ready"] == report["frozen_test_queries"] == 0
    assert dataset["judgments-reviewer-a.jsonl"] == dataset["judgments-reviewer-b.jsonl"]
    assert dataset["judgments-reviewer-a.jsonl"] is not dataset["judgments-reviewer-b.jsonl"]
    with pytest.raises(EvaluationBlocked):
        score(dataset["queries.review.jsonl"], [], [], package_sha256="a" * 64)


def test_protected_body_duplicate_is_quarantined_and_partial_future_are_not_sources(inputs):
    rows, policy = inputs
    duplicate = article("20261010ADUP000", "AI 完全不同的标题", body=rows[0]["body"])
    fragment = article("20261010AFRAG00", None, body=None, content_state="fragments_only",
                       url=None, publish_time=None, fragments=[{"chunk_index": 0, "text": "原始片段" * 40}])
    future = article("20261010AFUTURE", "AI 未来新闻", publish_time="2026-11-01T00:00:00+00:00")
    short = article("20261010ASHORT0", "AI 信息不足", body="短文")
    rows[1]["tags"]["topic_ids"] = ["health"]
    dataset, _ = build(rows + [duplicate, fragment, future, short], policy, {rows[0]["news_id"]})
    info = {r["news_id"]: r for r in dataset["inventory.jsonl"]}
    assert info[duplicate["news_id"]]["group_has_existing_eval_exposure"]
    assert info[rows[1]["news_id"]]["source_candidate"]  # Health word/tag is not an official board.
    for row in (fragment, future, short):
        assert not info[row["news_id"]]["content_candidate"]
    for q in dataset["queries.review.jsonl"]:
        assert q["source_news_id"] not in {rows[0]["news_id"], duplicate["news_id"]}
    assert next(r for r in dataset["evidence.jsonl"] if r["news_id"] == fragment["news_id"])["body"] is None


def test_ascii_keyword_boundaries_do_not_classify_chair_as_ai(inputs):
    _, policy = inputs
    assert not title_hits("chair company news", policy["topics"])
    assert title_hits("AI研究", policy["topics"])


def test_source_sampling_exclusions_do_not_remove_corpus_articles(inputs):
    rows, policy = inputs
    policy["topics"][0]["source_title_exclusions"] = ["火山", "海洋"]
    dataset, _ = build(rows, policy, set())
    assert not {r["source_news_id"] for r in dataset["queries.review.jsonl"]} & {
        rows[0]["news_id"], rows[1]["news_id"]}
    assert len(dataset["retrieval-corpus-membership.jsonl"]) == len(rows)


def test_normalized_body_copy_cannot_be_self_recall_under_another_id(inputs):
    rows, policy = inputs
    duplicate = article("20261010ACOPY00", "AI 源文章异名副本", body=rows[0]["body"] + "\n ")
    dataset, _ = build(rows + [duplicate], policy, set(), [qa_seed(rows[0])])
    pairs = [p for p in dataset["annotation.review.jsonl"] if p["source_news_id"] == rows[0]["news_id"]]
    assert all(p["news_id"] != duplicate["news_id"] for p in pairs)


def qa_seed(row):
    return {"case_id": "qa-1", "news_id": row["news_id"], "sampling_topic": "ai",
            "question": "据材料，原文内容是什么？", "candidate_answer": "待独立审核的候选回答",
            "evidence_quotes": [row["body"][:20]]}


def test_qa_requires_exact_quote_and_unexposed_source(inputs):
    rows, policy = inputs
    seed = qa_seed(rows[0])
    dataset, _ = build(rows, policy, set(), [seed])
    assert dataset["qa.review.jsonl"][0]["training_eligible"] is False
    with pytest.raises(ValueError, match="already exposed"):
        build(rows, policy, {rows[0]["news_id"]}, [seed])
    seed["evidence_quotes"] = ["凭空生成的证据"]
    with pytest.raises(ValueError, match="quote not in"):
        build(rows, policy, set(), [seed])


@pytest.mark.parametrize("mutation", [
    "body", "fragment", "self", "query_split", "judgment", "judgment_identity",
    "quality", "qa_ready", "qa_span", "qa_identity", "group", "membership",
])
def test_validator_rejects_silent_corruption_and_promotion(inputs, mutation):
    rows, policy = inputs
    dataset, _ = build(rows, policy, set(), [qa_seed(rows[0])])
    if mutation == "body":
        dataset["evidence.jsonl"][0]["body"] += "篡改"
    if mutation == "fragment":
        dataset["evidence.jsonl"][0]["fragments"] = [{"chunk_index": 9, "text": "注入片段"}]
    if mutation == "self":
        pair = dataset["annotation.review.jsonl"][0]
        pair["news_id"] = pair["source_news_id"]
    if mutation == "query_split":
        dataset["queries.review.jsonl"][0]["split"] = "test"
    if mutation == "judgment":
        dataset["judgments-reviewer-a.jsonl"][0]["grade"] = 3
    if mutation == "judgment_identity":
        dataset["judgments-reviewer-a.jsonl"][0]["news_id"] = "wrong"
    if mutation == "quality":
        dataset["quality.review.jsonl"][0]["index_recommendation"] = "include"
    if mutation == "qa_ready":
        dataset["qa.review.jsonl"][0]["training_eligible"] = True
    if mutation == "qa_span":
        dataset["qa.review.jsonl"][0]["evidence"][0]["start"] = -1
    if mutation == "qa_identity":
        dataset["qa.review.jsonl"][0]["evidence"][0]["record_sha256"] = "b" * 64
    if mutation == "group":
        dataset["proposed-groups.jsonl"][0]["news_ids"].append(rows[0]["news_id"])
    if mutation == "membership":
        dataset["retrieval-corpus-membership.jsonl"].pop()
    with pytest.raises(ValueError):
        validate_dataset(dataset)


def test_publish_verifies_bytes_and_never_overwrites_review_package(inputs, tmp_path):
    dataset, report = build(*inputs, set())
    output = tmp_path / "review"
    publish(output, dataset, report, {"test": True})
    assert verify(output)["status"] == "draft_pending_human_review"
    with pytest.raises(FileExistsError):
        publish(output, dataset, report, {"test": True})
    path = output / "evidence.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash/size"):
        verify(output)


def test_html_treats_news_as_text_and_json_rejects_ambiguous_input(inputs):
    rows, policy = inputs
    rows[0] = article(rows[0]["news_id"], rows[0]["title"], body="<script>alert('news')</script>" * 20)
    dataset, report = build(rows, policy, set(), [qa_seed(rows[0])])
    page = render_preview(dataset, report)
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    for text in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
        with pytest.raises(ValueError):
            strict_json(text)


@pytest.mark.parametrize("tamper", ["none", "body_with_new_export_hash", "tag_identity", "export_checksum"])
def test_loader_binds_tagged_export_to_immutable_source(inputs, tmp_path, tamper):
    rows, _ = inputs
    raw = {**rows[0], "priority": 0,
           "source": {"package": "test", "path": "source.jsonl", "file_sha256": "a" * 64}}
    package = build_package([raw], tenant_id="test")
    source = tmp_path / "source"
    save_package(package, source)
    row = deepcopy(package["versions"][0])
    row.update(tenant_id="test", release_id=package["manifest"]["release_id"],
               selection_quality_flags=package["items"][0]["quality_flags"])
    row["tags"] = {key: row[key] for key in ("news_id", "record_sha256", "body_sha256")}
    row["tags"]["tag_release_id"] = "tags-test"
    if tamper == "body_with_new_export_hash":
        row["body"] += "导出正文被修改"
    if tamper == "tag_identity":
        row["tags"]["record_sha256"] = "b" * 64
    export = tmp_path / "export.jsonl"
    export.write_text(canonical_json(row) + "\n", encoding="utf-8")
    manifest = {key: row[key] for key in ("tenant_id", "release_id")}
    manifest.update(sha256=file_hash(export), counts={"exported": 1}, tag_release_id="tags-test")
    if tamper == "export_checksum":
        manifest["sha256"] = "c" * 64
    path = tmp_path / "export.manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if tamper == "none":
        loaded, release = load_source(source, export, path)
        assert loaded == [row]
        assert release == package["manifest"]
    else:
        with pytest.raises(ValueError):
            load_source(source, export, path)
