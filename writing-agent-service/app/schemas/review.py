from typing import Literal
from pydantic import BaseModel, Field, model_validator
from app.schemas.research import ResearchPackage
from app.schemas.writing import (
    ArticleDraft,
    ArticleOutline,
    ArticleSection,
)

ReviewDecision = Literal[
    "approve",
    "rewrite",
    "research",
    "human_review"
]

IssueSeverity = Literal[
    "low",
    "medium",
    "high",
    "critical",
]

IssueType = Literal[
    "unsupported_claim",
    "citation_error",
    "time_inconsistency",
    "structure_error",
    "style_error",
    "risk_error",
]

class ReviewInput(BaseModel):
    """Orchestrator 传给 Reviewer Agent 的审核上下文。"""

    job_id: str = Field(min_length=1)
    review_round: int = Field(ge=1, le=3)
    research_package: ResearchPackage
    outline: ArticleOutline
    sections: list[ArticleSection] = Field(min_length=1)
    draft: ArticleDraft

    @model_validator(mode="after")
    def validate_context(self) -> "ReviewInput":
        """确保研究包、提纲、章节和全文属于同一任务。"""
        if self.research_package.job_id != self.job_id:
            raise ValueError( "research_package.job_id 与当前 job_id 不一致" )

        if self.outline.job_id != self.job_id:
            raise ValueError( "outline.job_id 与当前 job_id 不一致" )

        if self.draft.job_id != self.job_id:
            raise ValueError( "draft.job_id 与当前 job_id 不一致" )

        if any(
            section.job_id != self.job_id
            for section in self.sections
        ):
            raise ValueError("存在不属于当前任务的章节")

        outline_section_ids = {
            section.section_id
            for section in self.outline.sections
        }
        actual_section_ids = {
            section.section_id
            for section in self.sections
        }

        if outline_section_ids != actual_section_ids:
            raise ValueError( "审核章节与文章提纲中的章节不一致" )

        if set(self.draft.section_ids) != outline_section_ids:
            raise ValueError( "全文 section_ids 与文章提纲不一致" )

        known_facts = {
            fact.fact_id: str(fact.source_url)
            for fact in self.research_package.facts
        }

        for citation in self.draft.citations:
            expected_url = known_facts.get(citation.fact_id)

            if expected_url is None:
                raise ValueError(
                    "全文引用了研究包中不存在的事实: "
                    f"{citation.fact_id}"
                )

            if str(citation.source_url) != expected_url:
                raise ValueError(
                    "全文引用来源与研究包不一致: "
                    f"{citation.fact_id}"
                )

        return self

class ReviewIssue(BaseModel):
    """Reviewer Agent 发现一个具体问题。"""
    issue_id: str = Field(pattern=r"^I\d{3,}$")
    issue_type: IssueType
    severity: IssueSeverity
    section_id: str | None = Field(
        default=None,
        pattern=r"^S\d{2,}$"
    )
    fact_ids: list[str] = Field(default_factory=list)
    original_text: str | None = None
    reason: str = Field(min_length=1)
    suggested_action: ReviewDecision

class SectionReviewDecision(BaseModel):
    """Reviewer 对单个章节给出的审核结果。"""
    section_id: str = Field(pattern=r"^S\d{2,}$")
    decision: Literal["approve", "rewrite", "research"]
    issue_ids: list[str] = Field(default_factory=list)

class ReviewScores(BaseModel):
    """审核报告的各维度评分。"""
    factuality: int = Field(ge=0, le=100)
    citation: int = Field(ge=0, le=100)
    time_consistency: int = Field(ge=0, le=100)
    structure: int = Field(ge=0, le=100)
    style: int = Field(ge=0, le=100)
    risk: int = Field(ge=0, le=100)

class ReviewReport(BaseModel):
    """Reviewer Agent的结构化审核输出。"""
    job_id: str = Field(min_length=1)
    review_round: int = Field(ge=1, le=3)
    decision: ReviewDecision
    issues: list[ReviewIssue] = Field(default_factory=list)
    section_decisions: list[SectionReviewDecision] = Field(
        default_factory=list
    )
    scores: ReviewScores
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_review_result(self) -> "ReviewReport":
        """校验问题引用和最终审核决策的一致性。"""
        issue_ids = [
            issue.issue_id
            for issue in self.issues
        ]

        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("审核报告中的 issue_id 不能重复")

        section_ids = [ decision.section_id for decision in self.section_decisions ]

        if len(section_ids) != len(set(section_ids)):
            raise ValueError(
                "section_decisions 中的 section_id 不能重复"
            )

        known_issue_ids = set(issue_ids)

        referenced_issue_ids = {  
            issue_id
            for section in self.section_decisions
            for issue_id in section.issue_ids
        }

        unknown_issue_ids = (
            referenced_issue_ids - known_issue_ids
        )

        if unknown_issue_ids:
            unknown = ", ".join(sorted(unknown_issue_ids))
            raise ValueError( f"章节引用了不存在的 issue_id: {unknown}" )

        if self.decision == "approve":
            if self.issues:
                raise ValueError( "审核通过时 issues 必须为空" )

            if any(
                section.decision != "approve" for section in self.section_decisions
            ):
                raise ValueError( "审核通过时所有章节必须为 approve" )

        if self.decision == "rewrite":
            if not any(
                issue.suggested_action == "rewrite"
                for issue in self.issues
            ):
                raise ValueError(
                    "rewrite 决策必须包含需要改写的问题"
                )

        if self.decision == "research":
            if not any(
                issue.suggested_action == "research"
                for issue in self.issues
            ):
                raise ValueError(
                    "research 决策必须包含需要补充研究的问题"
                )

        if self.decision == "human_review":
            if not self.issues:
                raise ValueError(
                    "human_review 决策必须说明需要人工处理的问题"
                )

        return self
