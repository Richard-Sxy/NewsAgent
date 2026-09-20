"""运营推送领域的稳定契约。

这些对象只描述候选计划和可信校验上下文，不包含企业用户明细，也不代表
真实推送已经发生。
"""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.schemas.hot_news import HotNewsAnalysisReport, HotNewsMetrics


class PushPriority(str, Enum):
    S = "S"
    A = "A"
    B = "B"
    C = "C"


class PushChannel(str, Enum):
    APP_NOTIFICATION = "app_notification"
    IN_APP = "in_app"
    SMS = "sms"


class AudienceScope(str, Enum):
    TARGETED = "targeted"
    ALL_USERS = "all_users"


class PushPlanStatus(str, Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUDIENCE_RESOLVING = "audience_resolving"
    READY_TO_DISPATCH = "ready_to_dispatch"
    DISPATCHING = "dispatching"
    SENT = "sent"
    FAILED = "failed"
    EXPIRED = "expired"


class AudienceRule(BaseModel):
    """提交给企业人群服务的结构化条件，不展开为用户 ID。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: AudienceScope = AudienceScope.TARGETED
    region_codes: tuple[str, ...] = Field(default=(), max_length=100)
    interest_tags: tuple[str, ...] = Field(default=(), max_length=100)
    subscription_topics: tuple[str, ...] = Field(default=(), max_length=100)
    user_segments: tuple[str, ...] = Field(default=(), max_length=100)
    active_within_days: int | None = Field(default=None, ge=1, le=365)
    notification_enabled: bool = True

    @model_validator(mode="after")
    def validate_targeting(self) -> "AudienceRule":
        collections = (
            self.region_codes,
            self.interest_tags,
            self.subscription_topics,
            self.user_segments,
        )
        if any(len(items) != len(set(items)) for items in collections):
            raise ValueError("each audience dimension cannot contain duplicates")
        dimensions = tuple(item for items in collections for item in items)
        if self.scope == AudienceScope.ALL_USERS and dimensions:
            raise ValueError("all_users audience cannot contain targeting dimensions")
        if self.scope == AudienceScope.TARGETED and not dimensions:
            raise ValueError("targeted audience requires at least one dimension")
        return self


class PushPlan(BaseModel):
    """LLM 可以生成、但必须经过确定性校验和人工审核的候选计划。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: UUID
    analysis_run_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    news_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    priority: PushPriority
    audience_rule: AudienceRule
    channels: tuple[PushChannel, ...] = Field(min_length=1, max_length=3)
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    deep_link: str = Field(min_length=1, max_length=1000)
    send_time: AwareDatetime
    expire_at: AwareDatetime
    frequency_limit: int = Field(ge=1, le=20)
    requires_review: bool = True
    reason: str = Field(min_length=1, max_length=1000)
    evidence_news_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    strategy_version: str = Field(min_length=1, max_length=64)
    status: PushPlanStatus = PushPlanStatus.DRAFT

    @model_validator(mode="after")
    def validate_plan_shape(self) -> "PushPlan":
        if len(self.channels) != len(set(self.channels)):
            raise ValueError("channels cannot contain duplicates")
        if len(self.evidence_news_ids) != len(set(self.evidence_news_ids)):
            raise ValueError("evidence_news_ids cannot contain duplicates")
        if self.send_time >= self.expire_at:
            raise ValueError("expire_at must be later than send_time")
        return self


class PushAudienceOptions(BaseModel):
    """允许模型选择的人群维度白名单。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allow_all_users: bool = False
    region_codes: tuple[str, ...] = ()
    interest_tags: tuple[str, ...] = ()
    subscription_topics: tuple[str, ...] = ()
    user_segments: tuple[str, ...] = ()


class PushStrategyInput(BaseModel):
    """发送给策略模型的有界上下文，不包含审核结果或用户明细。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    news_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=2000)
    hot_score: float = Field(ge=0, le=1)
    metrics: HotNewsMetrics
    analysis: HotNewsAnalysisReport
    audience_options: PushAudienceOptions
    allowed_channels: tuple[PushChannel, ...] = Field(min_length=1, max_length=3)
    allowed_evidence_news_ids: tuple[str, ...] = Field(min_length=1, max_length=50)
    suggested_deep_link: str = Field(min_length=1, max_length=1000)
    earliest_send_time: AwareDatetime
    expire_by: AwareDatetime
    strategy_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_strategy_window(self) -> "PushStrategyInput":
        if self.earliest_send_time >= self.expire_by:
            raise ValueError("expire_by must be later than earliest_send_time")
        if len(self.allowed_channels) != len(set(self.allowed_channels)):
            raise ValueError("allowed_channels cannot contain duplicates")
        return self


class PushStrategyCandidate(BaseModel):
    """模型输出；身份、状态、版本和审核信息由可信业务层补齐。"""

    model_config = ConfigDict(extra="forbid")

    priority: PushPriority
    audience_rule: AudienceRule
    channels: tuple[PushChannel, ...] = Field(min_length=1, max_length=3)
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    deep_link: str = Field(min_length=1, max_length=1000)
    send_time: AwareDatetime
    expire_at: AwareDatetime
    frequency_limit: int = Field(ge=1, le=20)
    reason: str = Field(min_length=1, max_length=1000)
    evidence_news_ids: tuple[str, ...] = Field(min_length=1, max_length=20)


class PushPolicyContext(BaseModel):
    """由可信业务层提供的校验白名单，不能由 LLM 自行构造。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    news_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    allowed_evidence_news_ids: frozenset[str] = Field(min_length=1, max_length=50)
    allowed_region_codes: frozenset[str] = Field(default_factory=frozenset)
    allowed_interest_tags: frozenset[str] = Field(default_factory=frozenset)
    allowed_subscription_topics: frozenset[str] = Field(default_factory=frozenset)
    allowed_user_segments: frozenset[str] = Field(default_factory=frozenset)
    allow_all_users: bool = False
    allowed_channels: frozenset[PushChannel] = Field(
        default_factory=lambda: frozenset({PushChannel.IN_APP})
    )
    earliest_send_time: AwareDatetime | None = None
    latest_expire_at: AwareDatetime | None = None
    allowed_deep_link_prefixes: tuple[str, ...] = Field(
        default=("https://news.qq.com/",),
        min_length=1,
        max_length=20,
    )
    has_unconfirmed_facts: bool = False
    blocking_risk_labels: frozenset[str] = Field(default_factory=frozenset)
    approved_plan_id: UUID | None = None
    approved_strategy_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_approval_reference(self) -> "PushPolicyContext":
        if (self.approved_plan_id is None) != (
            self.approved_strategy_version is None
        ):
            raise ValueError(
                "approved_plan_id and approved_strategy_version must be set together"
            )
        return self


class PushPolicyDecision(str, Enum):
    PASSED = "passed"
    REQUIRES_REVIEW = "requires_review"
    BLOCKED = "blocked"


class PushPolicyViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    blocking: bool


class PushPolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: PushPolicyDecision
    checked_at: AwareDatetime
    policy_version: str = Field(min_length=1, max_length=64)
    violations: tuple[PushPolicyViolation, ...] = ()

    @property
    def can_continue(self) -> bool:
        return self.decision != PushPolicyDecision.BLOCKED

    @property
    def can_dispatch(self) -> bool:
        return self.decision == PushPolicyDecision.PASSED
