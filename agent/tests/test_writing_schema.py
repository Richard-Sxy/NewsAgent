import pytest
from pydantic import ValidationError

from app.schemas.writing import (
    ArticleAssemblyInput,
    ArticleDraft,
    ArticleOutline,
    ArticleSection,
    SectionWritingInput,
)
from tests.test_research_schema import valid_research_package


def valid_outline() -> dict:
    return {
        "job_id": "job_123",
        "title": "人工智能产业观察",
        "angle": "政策与产业的共同变化",
        "target_word_count": 1200,
        "sections": [
            {
                "section_id": "S01",
                "order": 1,
                "title": "政策背景",
                "purpose": "交代政策背景",
                "target_word_count": 400,
                "required_fact_ids": ["F001"],
            },
            {
                "section_id": "S02",
                "order": 2,
                "title": "行业影响",
                "purpose": "分析行业影响",
                "target_word_count": 800,
                "required_fact_ids": ["F002"],
            },
        ],
    }


def valid_section(section_id: str = "S01") -> dict:
    fact_id = "F001" if section_id == "S01" else "F002"
    return {
        "job_id": "job_123",
        "section_id": section_id,
        "title": "政策背景" if section_id == "S01" else "行业影响",
        "content": "经过事实约束的章节正文。",
        "used_fact_ids": [fact_id],
        "citations": [
            {
                "fact_id": fact_id,
                "source_url": f"https://example.com/news/{1 if fact_id == 'F001' else 2}",
            }
        ],
        "summary": "本节内容摘要。",
    }


def test_valid_outline_and_section_input() -> None:
    writing_input = SectionWritingInput.model_validate(
        {
            "job_id": "job_123",
            "section_id": "S02",
            "outline": valid_outline(),
            "research_package": valid_research_package(),
            "previous_sections_summary": {"S01": "第一节摘要"},
            "revision_instruction": None,
        }
    )
    assert writing_input.section_id == "S02"


def test_outline_requires_continuous_order() -> None:
    data = valid_outline()
    data["sections"][1]["order"] = 3
    with pytest.raises(ValidationError, match="连续排列"):
        ArticleOutline.model_validate(data)


def test_section_input_rejects_unknown_fact() -> None:
    data = valid_outline()
    data["sections"][0]["required_fact_ids"] = ["F999"]
    with pytest.raises(ValidationError, match="不存在的 fact_id: F999"):
        SectionWritingInput.model_validate(
            {
                "job_id": "job_123",
                "section_id": "S01",
                "outline": data,
                "research_package": valid_research_package(),
            }
        )


def test_section_input_rejects_future_section_summary() -> None:
    with pytest.raises(ValidationError, match="当前或后续章节: S02"):
        SectionWritingInput.model_validate(
            {
                "job_id": "job_123",
                "section_id": "S01",
                "outline": valid_outline(),
                "research_package": valid_research_package(),
                "previous_sections_summary": {"S02": "不应提前出现"},
            }
        )


def test_article_section_requires_citation_for_every_used_fact() -> None:
    data = valid_section()
    data["citations"] = []
    with pytest.raises(ValidationError, match="缺少引用: F001"):
        ArticleSection.model_validate(data)


def test_assembly_requires_all_outline_sections() -> None:
    with pytest.raises(ValidationError, match="缺少: S02"):
        ArticleAssemblyInput.model_validate(
            {
                "job_id": "job_123",
                "outline": valid_outline(),
                "sections": [valid_section("S01")],
            }
        )


def test_valid_assembly_input() -> None:
    assembly = ArticleAssemblyInput.model_validate(
        {
            "job_id": "job_123",
            "outline": valid_outline(),
            "sections": [valid_section("S01"), valid_section("S02")],
        }
    )
    assert len(assembly.sections) == 2


def test_draft_requires_fact_and_citation_consistency() -> None:
    with pytest.raises(ValidationError, match="不一致"):
        ArticleDraft.model_validate(
            {
                "job_id": "job_123",
                "title": "全文标题",
                "content": "完整稿件。",
                "section_ids": ["S01", "S02"],
                "used_fact_ids": ["F001"],
                "citations": [],
            }
        )
