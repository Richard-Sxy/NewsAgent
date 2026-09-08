"""热点分析大模型的稳定输入输出契约。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
)

from app.schemas.user_memory import (
    MemoryKind,
    MemoryTier,
    ResolvedMemoryOrigin,
)


MetricKey = Literal[
    "impressions",
    "clicks",
    "ctr",
    "unique_users",
    "effective_consumptions",
    "interactions",
    "hot_score",
    "click_component",
    "consumption_component",
    "interaction_component",
    "growth_component",
]

DriverType = Literal[
    "click",
    "consumption",
    "interaction",
    "growth",
    "insufficient_data",
]


class HotNewsMetrics(BaseModel):
    """由确定性计算层提供的权威行为指标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    impressions: int = Field(ge=0)
    clicks: int = Field(ge=0)
    ctr: float = Field(ge=0)
    unique_users: int = Field(ge=0)
    effective_consumptions: int = Field(ge=0)
    interactions: int = Field(ge=0)


class HotScoreComponents(BaseModel):
    """热点分数的确定性分量，模型只能解释，不能修改。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    click: float = Field(ge=0)
    consumption: float = Field(ge=0)
    interaction: float = Field(ge=0)
    growth: float = Field(ge=0)


class RelatedNewsEvidence(BaseModel):
    """允许大模型引用的一条关联新闻证据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    news_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    excerpt: str = Field(default="", max_length=2000)
    source_url: HttpUrl | None = None
    published_at: datetime | None = None
    final_score: float = Field(ge=0)
    rerank_reasons: list[str] = Field(default_factory=list, max_length=10)


class PromptMemoryItem(BaseModel):
    """一条经过 Resolver 选择、允许发送给模型的记忆。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: UUID
    memory_key: str = Field(min_length=1, max_length=128)
    memory_kind: MemoryKind
    selected_tier: MemoryTier
    origin: ResolvedMemoryOrigin
    summary: str = Field(min_length=1, max_length=500)
    value: JsonValue
    confidence: float = Field(ge=0, le=1)
    version: int = Field(ge=1)


class PromptMemoryContext(BaseModel):
    """一次模型调用允许使用的、大小受控的 Memory Context。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolver_policy_version: str = Field(min_length=1, max_length=64)
    resolved_at: AwareDatetime
    items: tuple[PromptMemoryItem, ...] = Field(default=(), max_length=20)
    omitted_memory_ids: tuple[UUID, ...] = Field(
        default=(),
        max_length=100,
    )


class HotNewsAnalysisInput(BaseModel):
    """发送给 FastGPT 热点分析 App 的最小可信上下文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    news_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=2000)
    content_excerpt: str = Field(default="", max_length=2000)
    content_type: Literal["article", "video"]
    window_start: datetime
    window_end: datetime
    hot_score: float = Field(ge=0, le=1)
    metrics: HotNewsMetrics
    score_components: HotScoreComponents
    related_news: list[RelatedNewsEvidence] = Field(default_factory=list, max_length=5)
    analysis_policy_version: str = Field(min_length=1, max_length=64)
    memory_context: PromptMemoryContext | None = None


class AnalysisReason(BaseModel):
    """一条可追溯的热点关注原因。"""

    model_config = ConfigDict(extra="forbid")

    reason_type: Literal["metric", "evidence", "hypothesis"]
    statement: str = Field(min_length=1, max_length=1000)
    metric_keys: list[MetricKey] = Field(default_factory=list, max_length=8)
    evidence_news_ids: list[str] = Field(default_factory=list, max_length=5)
    confidence: float = Field(ge=0, le=1)
    certainty: Literal["observed", "inferred"]


class RelatedNewsContext(BaseModel):
    """由输入证据支持的事件背景，不允许自由生成来源。"""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=1000)
    evidence_news_ids: list[str] = Field(min_length=1, max_length=5)
    confidence: float = Field(ge=0, le=1)


class OperationSuggestion(BaseModel):
    """供运营人员审核的建议，不代表自动执行。"""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=1000)
    priority: Literal["low", "medium", "high"]
    metric_keys: list[MetricKey] = Field(default_factory=list, max_length=8)
    evidence_news_ids: list[str] = Field(default_factory=list, max_length=5)


class HotNewsAnalysisReport(BaseModel):
    """热点分析 App 的结构化结果；成功结果不得退化为自由文本。"""

    model_config = ConfigDict(extra="forbid")

    news_id: str = Field(min_length=1, max_length=128)
    trend_assessment: str = Field(min_length=1, max_length=1500)
    dominant_driver: DriverType
    attention_reasons: list[AnalysisReason] = Field(default_factory=list, max_length=10)
    related_contexts: list[RelatedNewsContext] = Field(default_factory=list, max_length=10)
    operation_suggestions: list[OperationSuggestion] = Field(
        default_factory=list,
        max_length=10,
    )
    evidence_news_ids: list[str] = Field(default_factory=list, max_length=10)
    applied_memory_ids: list[UUID] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "本次报告实际使用的 memory_context.items[].memory_id；"
            "未使用记忆时必须为空，不得填写 omitted_memory_ids"
        ),
    )
    limitations: list[str] = Field(default_factory=list, max_length=10)
    overall_confidence: float = Field(ge=0, le=1)
    output_schema_version: Literal["1.0"] = "1.0"
