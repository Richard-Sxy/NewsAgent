import argparse
import json

import pytest

from scripts.evaluate_retrieval import (
    evaluate_retrieval,
    load_questions,
    parse_ks,
    rank_unique_collections,
    score_result,
)


def test_rank_unique_collections_uses_first_chunk_for_each_article():
    rows = [
        {"id": "d1", "collectionId": "c1", "q": "第一片", "chunkIndex": 0},
        {"id": "d2", "collectionId": "c1", "q": "第二片", "chunkIndex": 1},
        {"id": "d3", "collectionId": "c2", "q": "第三片", "chunkIndex": 0},
    ]

    ids, summaries = rank_unique_collections(rows)

    assert ids == ["c1", "c2"]
    assert [item["data_id"] for item in summaries] == ["d1", "d3"]
    assert [item["rank"] for item in summaries] == [1, 2]


def test_score_result_calculates_recall_and_reciprocal_rank():
    result = score_result(["target"], ["c1", "target", "c3"], (1, 3, 5))

    assert result["first_relevant_rank"] == 2
    assert result["reciprocal_rank"] == 0.5
    assert result["hits"] == {
        "recall_at_1": False,
        "recall_at_3": True,
        "recall_at_5": True,
    }


def test_evaluate_retrieval_counts_api_error_as_miss():
    questions = [
        {"id": "q1", "question": "命中", "expected_collection_ids": ["target"]},
        {"id": "q2", "question": "异常", "expected_collection_ids": ["other"]},
    ]

    def search(question):
        if question == "异常":
            raise RuntimeError("timeout")
        return [{"id": "d1", "collectionId": "target", "q": "正文"}]

    report = evaluate_retrieval(questions, search, request_interval=0)

    assert report["summary"] == {
        "total": 2,
        "completed": 1,
        "errors": 1,
        "recall_at_1": 0.5,
        "recall_at_3": 0.5,
        "recall_at_5": 0.5,
        "mrr": 0.5,
    }


def test_load_questions_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "questions.json"
    path.write_text(
        json.dumps(
            [
                {"id": "same", "question": "一", "expected_collection_ids": ["c1"]},
                {"id": "same", "question": "二", "expected_collection_ids": ["c2"]},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="id 重复"):
        load_questions(path)


def test_parse_ks():
    assert parse_ks("5,1,3,3") == (1, 3, 5)
    with pytest.raises(argparse.ArgumentTypeError):
        parse_ks("0,3")
