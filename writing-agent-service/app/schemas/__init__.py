"""新闻写作 Agent 的结构化数据契约。"""

from app.schemas.checkpoint import (
    ArtifactDescriptor,
    ArtifactReference,
    CheckpointCommit,
    FailureCommit,
    FailureOutcome,
    ResumePoint,
)
from app.schemas.hot_news import (
    AnalysisReason,
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
    HotNewsMetrics,
    HotScoreComponents,
    OperationSuggestion,
    RelatedNewsContext,
    RelatedNewsEvidence,
)
from app.schemas.research import ResearchPackage
from app.schemas.review import ReviewInput, ReviewReport
from app.schemas.writing import (
    ArticleAssemblyInput,
    ArticleDraft,
    ArticleOutline,
    ArticleSection,
    SectionWritingInput,
)

__all__ = [
    "ArticleAssemblyInput",
    "ArtifactDescriptor",
    "ArtifactReference",
    "ArticleDraft",
    "ArticleOutline",
    "ArticleSection",
    "CheckpointCommit",
    "FailureCommit",
    "FailureOutcome",
    "AnalysisReason",
    "HotNewsAnalysisInput",
    "HotNewsAnalysisReport",
    "HotNewsMetrics",
    "HotScoreComponents",
    "OperationSuggestion",
    "RelatedNewsContext",
    "RelatedNewsEvidence",
    "ResumePoint",
    "ResearchPackage",
    "ReviewInput",
    "ReviewReport",
    "SectionWritingInput",
]
