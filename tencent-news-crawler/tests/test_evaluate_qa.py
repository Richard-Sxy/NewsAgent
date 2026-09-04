import json

import pytest

from scripts.evaluate_qa import evaluate_questions, load_questions, score_answer


def test_score_answer_checks_keywords_source_and_length():
    item = {
        "should_refuse": False,
        "expected_keywords": ["宇树", "上市"],
        "expected_urls": ["https://example.com/news"],
        "max_answer_chars": 30,
    }
    result = score_answer(item, "宇树上市。来源：https://example.com/news")

    assert result["passed"] is True
    assert result["keyword_pass"] is True
    assert result["citation_pass"] is True
    assert result["length_pass"] is True


def test_score_answer_accepts_expected_refusal():
    item = {
        "should_refuse": True,
        "expected_keywords": [],
        "expected_urls": [],
    }

    result = score_answer(item, "当前知识库中没有足够的新闻资料回答这个问题。")

    assert result["passed"] is True
    assert result["refusal_pass"] is True


@pytest.mark.parametrize(
    "answer",
    [
        "根据现有资料无法确认该信息。",
        "我无法给出未来的准确结果。",
        "无法根据知识库预测开奖号码。",
    ],
)
def test_score_answer_accepts_common_refusal_phrases(answer):
    item = {
        "should_refuse": True,
        "expected_keywords": [],
        "expected_urls": [],
    }
    assert score_answer(item, answer)["passed"] is True


def test_evaluate_questions_records_success_and_request_error():
    questions = [
        {
            "id": "one",
            "category": "fact",
            "question": "问题一",
            "expected_keywords": ["答案"],
            "expected_urls": [],
            "should_refuse": False,
        },
        {
            "id": "two",
            "category": "fact",
            "question": "问题二",
            "expected_keywords": [],
            "expected_urls": [],
            "should_refuse": False,
        },
    ]

    def fake_ask(question):
        if question == "问题二":
            raise RuntimeError("模拟接口失败")
        return "这是答案。"

    report = evaluate_questions(questions, ask=fake_ask)

    assert report["summary"] == {
        "total": 2,
        "passed": 1,
        "failed": 1,
        "pass_rate": 0.5,
    }
    assert report["items"][1]["error"] == "模拟接口失败"


def test_load_questions_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "questions.json"
    item = {
        "id": "same",
        "category": "fact",
        "question": "问题",
        "expected_keywords": [],
        "should_refuse": False,
    }
    path.write_text(json.dumps([item, item]), encoding="utf-8")

    with pytest.raises(ValueError, match="id 重复"):
        load_questions(path)
