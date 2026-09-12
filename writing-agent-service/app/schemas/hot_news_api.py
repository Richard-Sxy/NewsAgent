"""热点运营控制台 API 的请求与响应契约。

数字字段（ctr、热度分数与各分量）在持久化 payload 中按字符串保存，
响应原样透传，前端不做二次计算；所有数值的权威来源是
``analysis_runs.result_payload`` 中的确定性计算结果。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.job_scenario import JobScenario
from app.schemas.analysis_feedback import FeedbackProblemType, FeedbackSeverity
from app.schemas.hot_news_decision import DecisionType
from app.schemas.job import WritingJobResponse


class HotNewsRunSummary(BaseModel):
    """一次已完成热点运行的索引信息。"""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    idempotency_key: str
    window_start: datetime
    window_end: datetime
    production_bundle_version: str
    workflow_version: str
    status: str
    fetched_record_count: int
    metric_snapshot_count: int
    ranked_news_count: int
    analyzed_news_count: int
    completed_at: datetime


class HotNewsRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: tuple[HotNewsRunSummary, ...]
    offset: int
    limit: int


class HotNewsMetricSnapshotView(BaseModel):
    """榜单项的确定性窗口指标；ctr 为字符串形式的精确小数。"""

    model_config = ConfigDict(extra="forbid")

    news_id: str
    content_type: str
    window_start: datetime
    window_end: datetime
    impressions: int
    clicks: int
    unique_users: int
    total_duration_seconds: int
    effective_consumptions: int
    interactions: int
    ctr: str


class HotScoreView(BaseModel):
    """热度分数与可解释分量；均为字符串形式的精确小数。"""

    model_config = ConfigDict(extra="forbid")

    score: str
    click_component: str
    consumption_component: str
    interaction_component: str
    growth_component: str


class HotNewsAnalysisSummaryView(BaseModel):
    """榜单项对应的大模型分析摘要（已通过业务校验的报告）。"""

    model_config = ConfigDict(extra="forbid")

    trend_assessment: str
    dominant_driver: str
    attention_reasons: tuple[dict[str, Any], ...]
    operation_suggestions: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...]
    evidence_news_ids: tuple[str, ...]
    overall_confidence: float
    fastgpt_request_id: str | None
    validated_at: datetime


class HotNewsRankedItemView(BaseModel):
    """榜单中的一项：指标、基线引用、热度分量与可选分析摘要。"""

    model_config = ConfigDict(extra="forbid")

    rank: int
    news_id: str
    metrics: HotNewsMetricSnapshotView
    baseline: dict[str, Any] | None
    hot_score: HotScoreView
    analysis: HotNewsAnalysisSummaryView | None


class HotNewsDecisionView(BaseModel):
    """一条运营决策记录。"""

    model_config = ConfigDict(extra="forbid")

    decision_id: UUID
    news_id: str
    decision_type: str
    reason: str
    correction_payload: dict[str, Any]
    operator_id: str
    idempotency_key: str
    supersedes_decision_id: UUID | None
    created_at: datetime


class HotNewsRunDetailResponse(BaseModel):
    """单次运行的完整视图：摘要 + 榜单 + 决策记录。"""

    model_config = ConfigDict(extra="forbid")

    run: HotNewsRunSummary
    ranked_news: tuple[HotNewsRankedItemView, ...]
    decisions: tuple[HotNewsDecisionView, ...]


class RecordHotNewsDecisionRequest(BaseModel):
    """运营决策请求；tenant/operator 只能来自网关注入的身份头。"""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    news_id: str = Field(min_length=1, max_length=128)
    decision_type: DecisionType
    reason: str = Field(min_length=1, max_length=128)
    correction_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=128)
    supersedes_decision_id: UUID | None = None
    feedback_problem_type: FeedbackProblemType = "analysis_incorrect"
    feedback_severity: FeedbackSeverity = "medium"


class RecordHotNewsDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: UUID
    decision_type: str
    status: Literal["recorded"]
    created: bool


class HandoffHotNewsToWritingRequest(BaseModel):
    """把一条已校验热点分析转交研究/写作流程的请求。"""

    model_config = ConfigDict(extra="forbid")

    scenario: JobScenario = JobScenario.ASSISTED_WRITING


class HotNewsWritingHandoffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: WritingJobResponse
    created: bool
