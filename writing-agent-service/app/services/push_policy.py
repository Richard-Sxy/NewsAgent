"""候选推送计划的确定性安全校验。"""

from dataclasses import dataclass
from datetime import datetime, timezone

from app.schemas.push import (
    AudienceScope,
    PushChannel,
    PushPlan,
    PushPolicyContext,
    PushPolicyDecision,
    PushPolicyResult,
    PushPolicyViolation,
    PushPriority,
)


@dataclass(frozen=True, slots=True)
class PushPolicyConfig:
    policy_version: str = "push-policy-v1"
    absolute_title_terms: tuple[str, ...] = (
        "绝对",
        "百分之百",
        "100%",
        "史上最",
        "必然",
    )

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("policy_version cannot be empty")


class PushPolicyValidator:
    """只使用可信上下文做规则判断，不让模型决定能否推送。"""

    def __init__(self, config: PushPolicyConfig | None = None) -> None:
        self.config = config or PushPolicyConfig()

    def validate(
        self,
        *,
        plan: PushPlan,
        context: PushPolicyContext,
        now: datetime | None = None,
        duplicate_detected: bool = False,
    ) -> PushPolicyResult:
        checked_at = now or datetime.now(timezone.utc)
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        violations: list[PushPolicyViolation] = []
        self._validate_identity(plan, context, violations)
        self._validate_evidence(plan, context, violations)
        self._validate_audience(plan, context, violations)
        self._validate_timing(plan, context, checked_at, violations)
        self._validate_content(plan, context, violations)
        self._validate_deep_link(plan, context, violations)
        self._validate_channels(plan, context, violations)

        if duplicate_detected:
            self._block(
                violations,
                "duplicate_delivery",
                "同事件、策略、人群版本和渠道的计划已存在",
            )

        needs_review = self.requires_manual_review(plan)
        if needs_review and not plan.requires_review:
            self._block(
                violations,
                "review_bypass",
                "高风险计划不得关闭人工审核",
            )
        elif needs_review:
            approval_matches = (
                context.approved_plan_id == plan.plan_id
                and context.approved_strategy_version == plan.strategy_version
            )
            has_approval = context.approved_plan_id is not None
            if has_approval and not approval_matches:
                self._block(
                    violations,
                    "stale_approval",
                    "人工审核记录与当前计划或策略版本不一致",
                )
            elif not approval_matches:
                violations.append(
                    PushPolicyViolation(
                        code="manual_review_required",
                        message="计划必须等待具备权限的运营人员审核",
                        blocking=False,
                    )
                )

        if any(item.blocking for item in violations):
            decision = PushPolicyDecision.BLOCKED
        elif any(item.code == "manual_review_required" for item in violations):
            decision = PushPolicyDecision.REQUIRES_REVIEW
        else:
            decision = PushPolicyDecision.PASSED

        return PushPolicyResult(
            decision=decision,
            checked_at=checked_at,
            policy_version=self.config.policy_version,
            violations=tuple(violations),
        )

    @staticmethod
    def requires_manual_review(plan: PushPlan) -> bool:
        return (
            plan.priority in {PushPriority.S, PushPriority.A}
            or plan.audience_rule.scope == AudienceScope.ALL_USERS
            or PushChannel.APP_NOTIFICATION in plan.channels
            or PushChannel.SMS in plan.channels
        )

    def _validate_identity(
        self,
        plan: PushPlan,
        context: PushPolicyContext,
        violations: list[PushPolicyViolation],
    ) -> None:
        for field in ("tenant_id", "news_id", "event_id"):
            if getattr(plan, field) != getattr(context, field):
                self._block(
                    violations,
                    f"{field}_mismatch",
                    f"计划 {field} 与可信热点上下文不一致",
                )

    def _validate_evidence(
        self,
        plan: PushPlan,
        context: PushPolicyContext,
        violations: list[PushPolicyViolation],
    ) -> None:
        unknown = set(plan.evidence_news_ids) - context.allowed_evidence_news_ids
        if unknown:
            self._block(
                violations,
                "unknown_evidence",
                f"计划引用了白名单外证据: {sorted(unknown)}",
            )

    def _validate_audience(
        self,
        plan: PushPlan,
        context: PushPolicyContext,
        violations: list[PushPolicyViolation],
    ) -> None:
        rule = plan.audience_rule
        if rule.scope == AudienceScope.ALL_USERS and not context.allow_all_users:
            self._block(
                violations,
                "all_users_out_of_scope",
                "可信业务上下文未授权全量用户范围",
            )
        checks = (
            ("region", rule.region_codes, context.allowed_region_codes),
            ("interest", rule.interest_tags, context.allowed_interest_tags),
            (
                "subscription_topic",
                rule.subscription_topics,
                context.allowed_subscription_topics,
            ),
            ("user_segment", rule.user_segments, context.allowed_user_segments),
        )
        for name, requested, allowed in checks:
            unknown = set(requested) - allowed
            if unknown:
                self._block(
                    violations,
                    f"audience_{name}_out_of_scope",
                    f"人群条件超出已确认范围: {sorted(unknown)}",
                )

    def _validate_timing(
        self,
        plan: PushPlan,
        context: PushPolicyContext,
        checked_at: datetime,
        violations: list[PushPolicyViolation],
    ) -> None:
        if plan.expire_at <= checked_at:
            self._block(violations, "plan_expired", "推送计划已经过期")
        if plan.send_time >= plan.expire_at:
            self._block(
                violations,
                "invalid_delivery_window",
                "发送时间必须早于失效时间",
            )
        if (
            context.earliest_send_time is not None
            and plan.send_time < context.earliest_send_time
        ):
            self._block(
                violations,
                "send_time_out_of_scope",
                "发送时间早于可信策略窗口",
            )
        if (
            context.latest_expire_at is not None
            and plan.expire_at > context.latest_expire_at
        ):
            self._block(
                violations,
                "expire_at_out_of_scope",
                "失效时间超出可信策略窗口",
            )

    def _validate_content(
        self,
        plan: PushPlan,
        context: PushPolicyContext,
        violations: list[PushPolicyViolation],
    ) -> None:
        if context.has_unconfirmed_facts:
            self._block(
                violations,
                "unconfirmed_facts",
                "包含未确认事实的计划不得进入投递",
            )
        if context.blocking_risk_labels:
            self._block(
                violations,
                "blocking_risk",
                "存在阻断型风险标签: "
                f"{sorted(context.blocking_risk_labels)}",
            )
        matched_terms = [
            term for term in self.config.absolute_title_terms if term in plan.title
        ]
        if matched_terms:
            self._block(
                violations,
                "absolute_title",
                f"标题包含绝对化表达: {matched_terms}",
            )

    def _validate_deep_link(
        self,
        plan: PushPlan,
        context: PushPolicyContext,
        violations: list[PushPolicyViolation],
    ) -> None:
        if not any(
            plan.deep_link.startswith(prefix)
            for prefix in context.allowed_deep_link_prefixes
        ):
            self._block(
                violations,
                "deep_link_out_of_scope",
                "跳转链接不在可信域名前缀白名单中",
            )

    def _validate_channels(
        self,
        plan: PushPlan,
        context: PushPolicyContext,
        violations: list[PushPolicyViolation],
    ) -> None:
        unknown = set(plan.channels) - context.allowed_channels
        if unknown:
            self._block(
                violations,
                "channel_out_of_scope",
                "推送渠道超出允许范围: "
                f"{sorted(channel.value for channel in unknown)}",
            )

    @staticmethod
    def _block(
        violations: list[PushPolicyViolation],
        code: str,
        message: str,
    ) -> None:
        violations.append(
            PushPolicyViolation(code=code, message=message, blocking=True)
        )
