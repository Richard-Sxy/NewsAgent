import pytest
from pydantic import ValidationError

from app.schemas.research import ResearchPackage


def valid_research_package() -> dict:
    return {
        "job_id": "job_123",
        "topic": "人工智能产业发展",
        "facts": [
            {
                "fact_id": "F001",
                "claim": "某项政策正式发布。",
                "evidence": "主管部门发布了政策全文。",
                "source_url": "https://example.com/news/1",
                "source_title": "政策发布",
                "published_at": "2026-08-26T08:00:00+08:00",
                "confidence": 0.95,
            },
            {
                "fact_id": "F002",
                "claim": "行业机构发布了不同预测。",
                "evidence": "报告给出了新的预测数字。",
                "source_url": "https://example.com/news/2",
                "source_title": "行业报告",
                "confidence": 0.8,
            },
        ],
        "timeline": [
            {
                "event_id": "T001",
                "occurred_at": "2026-08-26T08:00:00+08:00",
                "description": "政策发布",
                "supporting_fact_ids": ["F001"],
            }
        ],
        "conflicts": [
            {
                "conflict_id": "C001",
                "fact_ids": ["F001", "F002"],
                "description": "两份材料的预测口径不同。",
            }
        ],
        "evidence_gaps": [],
        "suggested_angles": [
            {
                "angle_id": "A001",
                "title": "政策对行业的影响",
                "rationale": "兼顾政策与行业观点。",
                "supporting_fact_ids": ["F001", "F002"],
            }
        ],
    }


def test_valid_research_package() -> None:
    package = ResearchPackage.model_validate(valid_research_package())
    assert package.facts[0].fact_id == "F001"
    assert str(package.facts[0].source_url) == "https://example.com/news/1"
    assert package.metrics is not None
    assert package.metrics.fact_count == 2
    assert package.metrics.unique_source_count == 2
    assert package.metrics.numeric_fact_count == 0
    assert package.metrics.citation_coverage_percent == 100
    assert package.metrics.average_confidence == 0.875


def test_agent_supplied_metrics_are_recomputed() -> None:
    data = valid_research_package()
    data["metrics"] = {
        "fact_count": 999,
        "unique_source_count": 999,
        "numeric_fact_count": 999,
        "citation_coverage_percent": 0,
        "average_confidence": 0,
        "timeline_event_count": 999,
        "conflict_count": 999,
        "evidence_gap_count": 999,
        "angle_count": 999,
    }

    package = ResearchPackage.model_validate(data)

    assert package.metrics is not None
    assert package.metrics.fact_count == 2
    assert package.metrics.unique_source_count == 2


def test_rejects_duplicate_fact_id() -> None:
    data = valid_research_package()
    data["facts"][1]["fact_id"] = "F001"

    with pytest.raises(ValidationError, match="fact_id 不能重复"):
        ResearchPackage.model_validate(data)


def test_rejects_unknown_fact_reference() -> None:
    data = valid_research_package()
    data["timeline"][0]["supporting_fact_ids"] = ["F999"]

    with pytest.raises(ValidationError, match="不存在的 fact_id: F999"):
        ResearchPackage.model_validate(data)


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_rejects_confidence_outside_unit_interval(confidence: float) -> None:
    data = valid_research_package()
    data["facts"][0]["confidence"] = confidence

    with pytest.raises(ValidationError):
        ResearchPackage.model_validate(data)


def test_rejects_invalid_fact_id_format() -> None:
    data = valid_research_package()
    data["facts"][0]["fact_id"] = "fact-1"

    with pytest.raises(ValidationError):
        ResearchPackage.model_validate(data)


def test_null_provenance_is_quarantined_as_evidence_gap() -> None:
    data = valid_research_package()
    data["facts"][0]["source_url"] = None
    data["timeline"][0]["occurred_at"] = None

    package = ResearchPackage.model_validate(data)

    assert [fact.fact_id for fact in package.facts] == ["F002"]
    assert package.timeline == []
    assert package.conflicts == []
    assert package.suggested_angles[0].supporting_fact_ids == ["F002"]
    assert len(package.evidence_gaps) == 2
    assert package.metrics is not None
    assert package.metrics.fact_count == 1
    assert package.metrics.evidence_gap_count == 2
