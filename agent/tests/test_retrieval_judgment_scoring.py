"""Independent-review, denominator and metric contract checks."""

from copy import deepcopy

import pytest

from tools.score_retrieval_judgments import EvaluationBlocked, score


PIN = "a" * 64


def fixture():
    queries = [{"query_id": "q1", "task": "related_news", "query": "事件进展", "source_news_id": "source",
                "status": "frozen", "human_reviewed": True, "expected_empty": False,
                "event_group_id": "event1", "split": "test", "judgment_scope": "pooled"}]
    qrels = [{"query_id": "q1", "news_id": nid, "status": "adjudicated", "final_grade": grade,
              "judgments": [{"reviewer_id": "a", "grade": grade}, {"reviewer_id": "b", "grade": grade}]}
             for nid, grade in (("relevant", 3), ("background", 1), ("irrelevant", 0))]
    predictions = [{"query_id": "q1", "package_manifest_sha256": PIN,
                    "news_ids": ["background", "relevant", "irrelevant"]}]
    return queries, qrels, predictions


def test_topic_background_is_not_binary_relevant_and_ndcg_is_explicit():
    result = score(*fixture(), package_sha256=PIN, ks=(3,))
    metrics = result["metrics"]
    assert metrics["precision@3"]["mean"] == pytest.approx(1 / 3)
    assert metrics["pooled_recall@3"]["mean"] == 1
    assert metrics["mrr@3"]["mean"] == .5
    assert 0 < metrics["pooled_ndcg_exp2@3"]["mean"] < 1
    assert metrics["precision@3"]["ci95"] is None
    assert result["acceptance_status"] == "not_approved"


@pytest.mark.parametrize("mutation", ["draft", "missing_prediction", "unjudged", "source", "duplicate", "identity", "single_reviewer", "disagreement"])
def test_rejects_unreviewed_missing_or_leaking_data(mutation):
    queries, qrels, predictions = fixture()
    if mutation == "draft": queries[0]["status"] = "draft"
    if mutation == "missing_prediction": predictions = []
    if mutation == "unjudged": predictions[0]["news_ids"] = ["unknown"]
    if mutation == "source": predictions[0]["news_ids"] = ["source"]
    if mutation == "duplicate": predictions[0]["news_ids"] = ["relevant", "relevant"]
    if mutation == "identity": predictions[0]["package_manifest_sha256"] = "b" * 64
    if mutation == "single_reviewer": qrels[0]["judgments"].pop()
    if mutation == "disagreement": qrels[0]["judgments"][0]["grade"] = 2
    with pytest.raises(EvaluationBlocked):
        score(queries, qrels, predictions, package_sha256=PIN)


def test_event_group_cannot_cross_splits():
    queries, qrels, predictions = fixture()
    other = {**deepcopy(queries[0]), "query_id": "q2", "split": "development"}
    with pytest.raises(EvaluationBlocked, match="leaks across splits"):
        score(queries + [other], qrels, predictions, package_sha256=PIN)


def test_no_answer_false_positive_and_correct_abstention_are_separate():
    queries, qrels, predictions = fixture()
    queries[0]["expected_empty"] = True
    queries[0]["corpus_no_answer_reviewed"] = True
    for row in qrels:
        row["final_grade"] = 0
        for vote in row["judgments"]: vote["grade"] = 0
    returned = score(queries, qrels, predictions, package_sha256=PIN)
    assert returned["no_answer"]["false_positive"]["mean"] == 1
    predictions[0]["news_ids"] = []
    empty = score(queries, qrels, predictions, package_sha256=PIN)
    assert empty["no_answer"]["correct_abstention"]["mean"] == 1
    assert empty["metrics"] == {}


def test_empty_positive_prediction_stays_in_fixed_denominator():
    queries, qrels, predictions = fixture()
    predictions[0]["news_ids"] = []
    result = score(queries, qrels, predictions, package_sha256=PIN, ks=(3,))
    assert result["denominator"] == 1
    assert result["metrics"]["pooled_recall@3"]["mean"] == 0


@pytest.mark.parametrize("field", ["query", "source_news_id"])
def test_frozen_related_query_requires_text_and_source(field):
    queries, qrels, predictions = fixture()
    queries[0].pop(field)
    with pytest.raises(EvaluationBlocked):
        score(queries, qrels, predictions, package_sha256=PIN)


def test_review_identity_whitespace_cannot_fake_two_reviewers():
    queries, qrels, predictions = fixture()
    qrels[0]["judgments"] = [{"reviewer_id": "same-human", "grade": 3},
                              {"reviewer_id": "same-human ", "grade": 3}]
    with pytest.raises(EvaluationBlocked):
        score(queries, qrels, predictions, package_sha256=PIN)


def test_question_source_seed_is_not_an_excluded_answer():
    queries, qrels, predictions = fixture()
    queries[0]["task"] = "question_search"
    with pytest.raises(EvaluationBlocked, match="hidden provenance"):
        score(queries, qrels, predictions, package_sha256=PIN)
