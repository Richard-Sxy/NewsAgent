import pytest
from pydantic import ValidationError

from app.schemas.review import ReviewInput, ReviewReport
from tests.test_research_schema import valid_research_package
from tests.test_writing_schema import valid_outline, valid_section


def valid_draft() -> dict:
    return {
        "job_id": "job_123",
        "title": "人工智能产业观察",
        "content": "合成后的完整新闻稿。",
        "section_ids": ["S01", "S02"],
        "used_fact_ids": ["F001", "F002"],
        "citations": [
            {
                "fact_id": "F001",
                "source_url": "https://example.com/news/1",
            },
            {
                "fact_id": "F002",
                "source_url": "https://example.com/news/2",
            },
        ],
    }


def valid_scores() -> dict:
    return {
        "factuality": 95,
        "citation": 95,
        "time_consistency": 90,
        "structure": 90,
        "style": 88,
        "risk": 96,
    }


def test_valid_review_input() -> None:
    value = ReviewInput.model_validate(
        {
            "job_id": "job_123",
            "review_round": 1,
            "research_package": valid_research_package(),
            "outline": valid_outline(),
            "sections": [valid_section("S01"), valid_section("S02")],
            "draft": valid_draft(),
        }
    )
    assert value.review_round == 1


def test_review_input_rejects_wrong_source_url() -> None:
    draft = valid_draft()
    draft["citations"][0]["source_url"] = "https://example.com/wrong"
    with pytest.raises(ValidationError, match="引用来源与研究包不一致: F001"):
        ReviewInput.model_validate(
            {
                "job_id": "job_123",
                "review_round": 1,
                "research_package": valid_research_package(),
                "outline": valid_outline(),
                "sections": [valid_section("S01"), valid_section("S02")],
                "draft": draft,
            }
        )


def test_review_round_cannot_exceed_readme_limit() -> None:
    with pytest.raises(ValidationError):
        ReviewInput.model_validate(
            {
                "job_id": "job_123",
                "review_round": 4,
                "research_package": valid_research_package(),
                "outline": valid_outline(),
                "sections": [valid_section("S01"), valid_section("S02")],
                "draft": valid_draft(),
            }
        )


def test_valid_approve_report() -> None:
    report = ReviewReport.model_validate(
        {
            "job_id": "job_123",
            "review_round": 1,
            "decision": "approve",
            "issues": [],
            "section_decisions": [
                {"section_id": "S01", "decision": "approve", "issue_ids": []},
                {"section_id": "S02", "decision": "approve", "issue_ids": []},
            ],
            "scores": valid_scores(),
            "summary": "稿件通过审核。",
        }
    )
    assert report.decision == "approve"


def test_approve_report_cannot_contain_issues() -> None:
    with pytest.raises(ValidationError, match="issues 必须为空"):
        ReviewReport.model_validate(
            {
                "job_id": "job_123",
                "review_round": 1,
                "decision": "approve",
                "issues": [
                    {
                        "issue_id": "I001",
                        "issue_type": "style_error",
                        "severity": "low",
                        "section_id": "S01",
                        "reason": "表达重复。",
                        "suggested_action": "rewrite",
                    }
                ],
                "section_decisions": [],
                "scores": valid_scores(),
                "summary": "存在问题。",
            }
        )


def test_rewrite_requires_matching_suggested_action() -> None:
    with pytest.raises(ValidationError, match="rewrite 决策必须"):
        ReviewReport.model_validate(
            {
                "job_id": "job_123",
                "review_round": 1,
                "decision": "rewrite",
                "issues": [
                    {
                        "issue_id": "I001",
                        "issue_type": "unsupported_claim",
                        "severity": "high",
                        "section_id": "S01",
                        "reason": "缺少证据。",
                        "suggested_action": "research",
                    }
                ],
                "section_decisions": [],
                "scores": valid_scores(),
                "summary": "需要处理。",
            }
        )


def test_section_decision_rejects_unknown_issue() -> None:
    with pytest.raises(ValidationError, match="不存在的 issue_id: I999"):
        ReviewReport.model_validate(
            {
                "job_id": "job_123",
                "review_round": 1,
                "decision": "rewrite",
                "issues": [
                    {
                        "issue_id": "I001",
                        "issue_type": "style_error",
                        "severity": "medium",
                        "section_id": "S01",
                        "reason": "表达需要调整。",
                        "suggested_action": "rewrite",
                    }
                ],
                "section_decisions": [
                    {
                        "section_id": "S01",
                        "decision": "rewrite",
                        "issue_ids": ["I999"],
                    }
                ],
                "scores": valid_scores(),
                "summary": "需要改写。",
            }
        )
