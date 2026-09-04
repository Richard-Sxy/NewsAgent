from scripts.generate_retrieval_questions import (
    build_questions,
    classify_question_intent,
    questions_are_similar,
    specificity_score,
)

# 这边是测评脚本
def test_build_questions_maps_original_collection_and_deduplicates_articles():
    mapping = {
        "qa-1": {
            "collection_id": "news-1",
            "url": "https://example.com/1",
            "title": "新闻一",
            "topic": "科技",
            "publish_time": "2026-08-23 10:00:00",
        },
        "qa-2": {
            "collection_id": "news-2",
            "url": "https://example.com/2",
            "title": "新闻二",
            "topic": "体育",
            "publish_time": "2026-08-23 11:00:00",
        },
    }
    qa_rows = [
        {"collection_id": "qa-1", "question": "问题一？", "answer": "答案一"},
        {"collection_id": "qa-1", "question": "问题一之二？", "answer": "答案二"},
        {"collection_id": "qa-2", "question": "问题二？", "answer": "答案三"},
        {"collection_id": "unknown", "question": "无映射？", "answer": "忽略"},
    ]

    result = build_questions(qa_rows, mapping, limit=10, seed=1)

    assert len(result) == 2
    assert {item["expected_collection_ids"][0] for item in result} == {
        "news-1",
        "news-2",
    }
    assert {item["qa_collection_id"] for item in result} == {"qa-1", "qa-2"}
    assert [item["id"] for item in result] == ["retrieval_0001", "retrieval_0002"]


def test_build_questions_ignores_empty_qa_and_honors_limit():
    mapping = {
        f"qa-{index}": {
            "collection_id": f"news-{index}",
            "url": f"https://example.com/{index}",
            "title": f"新闻{index}",
            "topic": "科技",
            "publish_time": None,
        }
        for index in range(3)
    }
    qa_rows = [
        {"collection_id": "qa-0", "question": "", "answer": "答案"},
        {"collection_id": "qa-1", "question": "问题一？", "answer": "答案一"},
        {"collection_id": "qa-2", "question": "问题二？", "answer": "答案二"},
    ]

    result = build_questions(qa_rows, mapping, limit=1, seed=1)

    assert len(result) == 1
    assert result[0]["question"] in {"问题一？", "问题二？"}


def test_specific_question_is_preferred_over_generic_financial_report_question():
    generic = {
        "question": "腾讯于何时发布了2026年第二季度财报？",
        "answer": "2026年8月12日。",
    }
    specific = {
        "question": "WorkBuddy率先在哪些系统及终端上实现全覆盖？",
        "answer": "iOS、Windows、Android、鸿蒙等多系统及终端。",
    }

    assert specificity_score(specific) > specificity_score(generic)


def test_classifies_different_semantic_intents():
    assert classify_question_intent("柯洁为什么认为人类能够战胜围棋AI？") == "causal_reasoning"
    assert classify_question_intent("两款车型相比有哪些差异？") == "comparison"
    assert classify_question_intent("刘雨辰如何完成两个进球？") == "mechanism_process"
    assert classify_question_intent("比赛何时举行？") == "time_event"
    assert classify_question_intent("营业收入是多少？") == "numeric_fact"
    assert classify_question_intent("决赛对手是谁？") == "person_entity"


def test_detects_template_like_questions_but_keeps_distinct_semantics():
    assert questions_are_similar(
        "腾讯2025年第二季度营业收入是多少？",
        "腾讯2026年第二季度营业收入是多少？",
    )
    assert not questions_are_similar(
        "腾讯第二季度营业收入是多少？",
        "腾讯为什么加大人工智能投入？",
    )


def test_build_questions_adds_intent_and_removes_similar_templates():
    mapping = {
        "qa-1": {
            "collection_id": "news-1",
            "url": "https://example.com/1",
            "title": "新闻一",
            "topic": "财经",
            "publish_time": None,
        },
        "qa-2": {
            "collection_id": "news-2",
            "url": "https://example.com/2",
            "title": "新闻二",
            "topic": "财经",
            "publish_time": None,
        },
    }
    rows = [
        {"collection_id": "qa-1", "question": "腾讯2025年第二季度营业收入是多少？", "answer": "一百亿元"},
        {"collection_id": "qa-2", "question": "腾讯2026年第二季度营业收入是多少？", "answer": "两百亿元"},
    ]

    result = build_questions(rows, mapping, limit=10, seed=1)

    assert len(result) == 1
    assert result[0]["intent"] == "numeric_fact"
