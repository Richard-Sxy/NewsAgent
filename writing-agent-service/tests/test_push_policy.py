from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.push import (
    AudienceRule,
    AudienceScope,
    PushChannel,
    PushPlan,
    PushPlanStatus,
    PushPolicyContext,
    PushPolicyDecision,
    PushPriority,
)
from app.services.push_policy import PushPolicyValidator


NOW = datetime(2026, 9, 19, 8, tzinfo=timezone.utc)


def make_plan(**overrides) -> PushPlan:
    values = {
        "plan_id": uuid4(),
        "analysis_run_id": uuid4(),
        "tenant_id": "tenant-1",
        "news_id": "news-1",
        "event_id": "event-1",
        "priority": PushPriority.B,
        "audience_rule": AudienceRule(
            interest_tags=("ai",),
            active_within_days=30,
        ),
        "channels": (PushChannel.IN_APP,),
        "title": "AI 数据中心进入新的发展阶段",
        "summary": "基于热点指标和相关新闻证据生成的候选摘要。",
        "deep_link": "https://news.qq.com/rain/a/news-1",
        "send_time": NOW + timedelta(minutes=10),
        "expire_at": NOW + timedelta(hours=2),
        "frequency_limit": 1,
        "requires_review": True,
        "reason": "热点增速较快，适合关注 AI 基础设施的用户。",
        "evidence_news_ids": ("evidence-1",),
        "strategy_version": "push-strategy-v1",
        "status": PushPlanStatus.DRAFT,
    }
    values.update(overrides)
    return PushPlan(**values)


def make_context(**overrides) -> PushPolicyContext:
    values = {
        "tenant_id": "tenant-1",
        "news_id": "news-1",
        "event_id": "event-1",
        "allowed_evidence_news_ids": frozenset({"evidence-1", "evidence-2"}),
        "allowed_interest_tags": frozenset({"ai", "technology"}),
        "evaluated_at": NOW,
    }
    values.update(overrides)
    return PushPolicyContext(**values)


def violation_codes(result) -> set[str]:
    return {item.code for item in result.violations}


def test_low_risk_targeted_plan_passes():
    result = PushPolicyValidator().validate(
        plan=make_plan(),
        context=make_context(),
        now=NOW,
    )

    assert result.decision == PushPolicyDecision.PASSED
    assert result.can_dispatch is True
    assert result.violations == ()


def test_high_priority_plan_waits_for_human_review():
    result = PushPolicyValidator().validate(
        plan=make_plan(priority=PushPriority.A),
        context=make_context(),
        now=NOW,
    )

    assert result.decision == PushPolicyDecision.REQUIRES_REVIEW
    assert result.can_continue is True
    assert result.can_dispatch is False
    assert violation_codes(result) == {"manual_review_required"}


def test_approved_high_priority_plan_passes_policy():
    plan = make_plan(
        priority=PushPriority.A,
        status=PushPlanStatus.APPROVED,
    )
    result = PushPolicyValidator().validate(
        plan=plan,
        context=make_context(
            approved_plan_id=plan.plan_id,
            approved_strategy_version=plan.strategy_version,
        ),
        now=NOW,
    )

    assert result.decision == PushPolicyDecision.PASSED


def test_candidate_cannot_claim_approved_without_trusted_approval_record():
    result = PushPolicyValidator().validate(
        plan=make_plan(
            priority=PushPriority.A,
            status=PushPlanStatus.APPROVED,
        ),
        context=make_context(),
        now=NOW,
    )

    assert result.decision == PushPolicyDecision.REQUIRES_REVIEW


def test_changed_strategy_invalidates_old_approval():
    plan = make_plan(priority=PushPriority.A)
    result = PushPolicyValidator().validate(
        plan=plan,
        context=make_context(
            approved_plan_id=plan.plan_id,
            approved_strategy_version="older-strategy",
        ),
        now=NOW,
    )

    assert result.decision == PushPolicyDecision.BLOCKED
    assert "stale_approval" in violation_codes(result)


def test_high_risk_plan_cannot_disable_review():
    result = PushPolicyValidator().validate(
        plan=make_plan(
            priority=PushPriority.S,
            requires_review=False,
        ),
        context=make_context(),
        now=NOW,
    )

    assert result.decision == PushPolicyDecision.BLOCKED
    assert "review_bypass" in violation_codes(result)


@pytest.mark.parametrize(
    ("plan_overrides", "context_overrides", "expected_code"),
    [
        ({"tenant_id": "other"}, {}, "tenant_id_mismatch"),
        (
            {"evidence_news_ids": ("invented",)},
            {},
            "unknown_evidence",
        ),
        (
            {},
            {"has_unconfirmed_facts": True},
            "unconfirmed_facts",
        ),
        (
            {},
            {"blocking_risk_labels": frozenset({"legal"})},
            "blocking_risk",
        ),
    ],
)
def test_untrusted_or_risky_plan_is_blocked(
    plan_overrides,
    context_overrides,
    expected_code,
):
    result = PushPolicyValidator().validate(
        plan=make_plan(**plan_overrides),
        context=make_context(**context_overrides),
        now=NOW,
    )

    assert result.decision == PushPolicyDecision.BLOCKED
    assert expected_code in violation_codes(result)


def test_out_of_scope_audience_and_duplicate_are_blocked():
    result = PushPolicyValidator().validate(
        plan=make_plan(
            audience_rule=AudienceRule(interest_tags=("finance",)),
        ),
        context=make_context(),
        now=NOW,
        duplicate_detected=True,
    )

    assert result.decision == PushPolicyDecision.BLOCKED
    assert violation_codes(result) == {
        "audience_interest_out_of_scope",
        "duplicate_delivery",
    }


def test_expired_plan_and_absolute_title_are_blocked():
    result = PushPolicyValidator().validate(
        plan=make_plan(
            title="史上最强 AI 产品必然成功",
            send_time=NOW - timedelta(hours=2),
            expire_at=NOW - timedelta(hours=1),
        ),
        context=make_context(),
        now=NOW,
    )

    assert result.decision == PushPolicyDecision.BLOCKED
    assert violation_codes(result) == {"plan_expired", "absolute_title"}


def test_untrusted_deep_link_is_blocked():
    result = PushPolicyValidator().validate(
        plan=make_plan(deep_link="https://example.invalid/phishing"),
        context=make_context(),
        now=NOW,
    )

    assert result.decision == PushPolicyDecision.BLOCKED
    assert "deep_link_out_of_scope" in violation_codes(result)


def test_app_notification_requires_review_even_for_b_priority():
    result = PushPolicyValidator().validate(
        plan=make_plan(channels=(PushChannel.APP_NOTIFICATION,)),
        context=make_context(
            allowed_channels=frozenset({PushChannel.APP_NOTIFICATION})
        ),
        now=NOW,
    )

    assert result.decision == PushPolicyDecision.REQUIRES_REVIEW


def test_targeted_audience_requires_a_dimension():
    with pytest.raises(ValidationError, match="requires at least one dimension"):
        AudienceRule(scope=AudienceScope.TARGETED)


def test_all_users_cannot_smuggle_targeting_dimensions():
    with pytest.raises(ValidationError, match="cannot contain targeting"):
        AudienceRule(
            scope=AudienceScope.ALL_USERS,
            region_codes=("CN",),
        )


def test_naive_validation_clock_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        PushPolicyValidator().validate(
            plan=make_plan(),
            context=make_context(),
            now=datetime(2026, 9, 19, 8),
        )
