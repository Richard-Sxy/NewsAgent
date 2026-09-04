from enum import Enum


class StepType(str, Enum):
    RESEARCH = "research"
    OUTLINE = "outline"
    SECTION_DRAFT = "section_draft"
    ASSEMBLE = "assemble"
    REVIEW = "review"
    SECTION_REVISE = "section_revise"
    FINALIZE = "finalize"


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactType(str, Enum):
    RESEARCH_PACKAGE = "research_package"
    OUTLINE = "outline"
    SECTION = "section"
    DRAFT = "draft"
    REVIEW_REPORT = "review_report"
    FINAL_ARTICLE = "final_article"


class AgentType(str, Enum):
    RESEARCH = "research"
    WRITER = "writer"
    REVIEWER = "reviewer"
    SUPERVISOR = "supervisor"
