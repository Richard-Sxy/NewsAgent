from typing import Any

from pydantic import BaseModel, Field, HttpUrl, model_validator

from app.schemas.research import ResearchPackage


class OutlineSection(BaseModel):
    """文章提纲中的一个章节计划。"""

    section_id: str = Field(pattern=r"^S\d{2,}$")
    order: int = Field(ge=1)
    title: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    target_word_count: int = Field(ge=100)
    required_fact_ids: list[str] = Field(default_factory=list)


class ArticleOutline(BaseModel):
    """Writer Agent 生成的文章提纲。"""

    job_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    angle: str = Field(min_length=1)
    target_word_count: int = Field(ge=300)
    sections: list[OutlineSection] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sections(self) -> "ArticleOutline":
        section_ids = [section.section_id for section in self.sections]
        orders = [section.order for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("提纲中的 section_id 不能重复")
        if len(orders) != len(set(orders)):
            raise ValueError("提纲中的章节顺序不能重复")
        if sorted(orders) != list(range(1, len(self.sections) + 1)):
            raise ValueError("章节顺序必须从 1 开始连续排列")
        for section in self.sections:
            if len(section.required_fact_ids) != len(set(section.required_fact_ids)):
                raise ValueError(
                    f"章节 {section.section_id} 的 required_fact_ids 不能重复"
                )
        return self


class SectionWritingInput(BaseModel):
    """Python Orchestrator 传给 Writer Agent 的章节任务。"""

    job_id: str = Field(min_length=1)
    section_id: str = Field(pattern=r"^S\d{2,}$")
    outline: ArticleOutline
    research_package: ResearchPackage
    requirements: dict[str, Any] = Field(default_factory=dict)
    previous_sections_summary: dict[str, str] = Field(default_factory=dict)
    current_section: "ArticleSection | None" = None
    revision_instruction: str | None = None

    @model_validator(mode="after")
    def validate_context(self) -> "SectionWritingInput":
        if self.outline.job_id != self.job_id:
            raise ValueError("outline.job_id 与当前 job_id 不一致")
        if self.research_package.job_id != self.job_id:
            raise ValueError("research_package.job_id 与当前 job_id 不一致")
        if self.current_section is not None:
            if self.current_section.job_id != self.job_id:
                raise ValueError("current_section.job_id 与当前 job_id 不一致")
            if self.current_section.section_id != self.section_id:
                raise ValueError("current_section.section_id 与当前章节不一致")

        sections_by_id = {
            section.section_id: section for section in self.outline.sections
        }
        target_section = sections_by_id.get(self.section_id)
        if target_section is None:
            raise ValueError("section_id 不存在于文章提纲中")

        unknown_summaries = set(self.previous_sections_summary) - set(sections_by_id)
        if unknown_summaries:
            unknown = ", ".join(sorted(unknown_summaries))
            raise ValueError(f"前文摘要包含未知章节: {unknown}")
        prior_ids = {
            section.section_id
            for section in self.outline.sections
            if section.order < target_section.order
        }
        non_prior = set(self.previous_sections_summary) - prior_ids
        if non_prior:
            invalid = ", ".join(sorted(non_prior))
            raise ValueError(f"前文摘要包含当前或后续章节: {invalid}")

        known_facts = {fact.fact_id for fact in self.research_package.facts}
        unknown_facts = set(target_section.required_fact_ids) - known_facts
        if unknown_facts:
            unknown = ", ".join(sorted(unknown_facts))
            raise ValueError(f"章节引用了不存在的 fact_id: {unknown}")
        return self


class SectionCitation(BaseModel):
    fact_id: str = Field(pattern=r"^F\d{3,}$")
    source_url: HttpUrl


class ArticleSection(BaseModel):
    """Writer Agent 完成的单个章节。"""

    job_id: str = Field(min_length=1)
    section_id: str = Field(pattern=r"^S\d{2,}$")
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    used_fact_ids: list[str] = Field(default_factory=list)
    citations: list[SectionCitation] = Field(default_factory=list)
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_citations(self) -> "ArticleSection":
        if len(self.used_fact_ids) != len(set(self.used_fact_ids)):
            raise ValueError("used_fact_ids 不能重复")
        cited = [citation.fact_id for citation in self.citations]
        if len(cited) != len(set(cited)):
            raise ValueError("同一个 fact_id 不能重复引用")
        if set(cited) - set(self.used_fact_ids):
            unknown = ", ".join(sorted(set(cited) - set(self.used_fact_ids)))
            raise ValueError(f"引用事实未出现在 used_fact_ids 中: {unknown}")
        if set(self.used_fact_ids) - set(cited):
            missing = ", ".join(sorted(set(self.used_fact_ids) - set(cited)))
            raise ValueError(f"used_fact_ids 缺少引用: {missing}")
        return self


class ArticleAssemblyInput(BaseModel):
    job_id: str = Field(min_length=1)
    outline: ArticleOutline
    sections: list[ArticleSection] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sections(self) -> "ArticleAssemblyInput":
        if self.outline.job_id != self.job_id:
            raise ValueError("outline.job_id 与当前 job_id 不一致")
        if any(section.job_id != self.job_id for section in self.sections):
            raise ValueError("存在不属于当前任务的章节")
        expected = {section.section_id for section in self.outline.sections}
        actual_ids = [section.section_id for section in self.sections]
        if len(actual_ids) != len(set(actual_ids)):
            raise ValueError("待合成章节的 section_id 不能重复")
        actual = set(actual_ids)
        if expected != actual:
            missing = ", ".join(sorted(expected - actual)) or "无"
            extra = ", ".join(sorted(actual - expected)) or "无"
            raise ValueError(f"章节集合不完整，缺少: {missing}；多余: {extra}")
        return self


class ArticleDraft(BaseModel):
    job_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    section_ids: list[str] = Field(min_length=1)
    used_fact_ids: list[str] = Field(default_factory=list)
    citations: list[SectionCitation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identifiers_and_citations(self) -> "ArticleDraft":
        if len(self.section_ids) != len(set(self.section_ids)):
            raise ValueError("全文 section_ids 不能重复")
        if len(self.used_fact_ids) != len(set(self.used_fact_ids)):
            raise ValueError("全文 used_fact_ids 不能重复")
        cited = [citation.fact_id for citation in self.citations]
        if len(cited) != len(set(cited)):
            raise ValueError("全文中的同一个 fact_id 不能重复引用")
        if set(cited) != set(self.used_fact_ids):
            raise ValueError("全文 used_fact_ids 与 citations 不一致")
        return self
