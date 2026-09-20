from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.clients.fastgpt import AgentResult
from app.schemas.hot_news import (
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
    HotNewsMetrics,
    HotScoreComponents,
)
from app.schemas.push import (
    AudienceRule,
    PushChannel,
    PushPolicyContext,
    PushPolicyDecision,
    PushPriority,
    PushStrategyCandidate,
    PushStrategyInput,
)
from app.services.agents.push_strategy import PushStrategyAgentRunner
from app.services.push_policy import PushPolicyValidator
from app.services.push_strategy import PushStrategyInputBuilder, PushStrategyService


NOW = datetime(2026, 9, 19, 8, tzinfo=timezone.utc)


def analysis_input() -> HotNewsAnalysisInput:
    return HotNewsAnalysisInput(
        news_id="news-1",
        title="AI 数据中心进入新阶段",
        summary="用户关注度持续增长。",
        content_type="article",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        hot_score=0.72,
        metrics=HotNewsMetrics(
            impressions=1000,
            clicks=400,
            ctr=0.4,
            unique_users=350,
            effective_consumptions=200,
            interactions=60,
        ),
        score_components=HotScoreComponents(
            click=0.3,
            consumption=0.2,
            interaction=0.1,
            growth=0.12,
        ),
        analysis_policy_version="hot-news-v1",
    )


def report() -> HotNewsAnalysisReport:
    return HotNewsAnalysisReport(
        news_id="news-1",
        trend_assessment="关注度正在上升。",
        dominant_driver="click",
        evidence_news_ids=["evidence-1"],
        applied_memory_ids=[],
        limitations=[],
        overall_confidence=0.85,
    )


def context(**overrides) -> PushPolicyContext:
    values = {
        "tenant_id": "tenant-1",
        "news_id": "news-1",
        "event_id": "event-1",
        "allowed_evidence_news_ids": frozenset({"evidence-1"}),
        "allowed_interest_tags": frozenset({"ai"}),
        "allowed_channels": frozenset({PushChannel.IN_APP}),
        "earliest_send_time": NOW + timedelta(minutes=5),
        "latest_expire_at": NOW + timedelta(hours=2),
        "evaluated_at": NOW,
    }
    values.update(overrides)
    return PushPolicyContext(**values)


def strategy_input() -> PushStrategyInput:
    return PushStrategyInputBuilder().build(
        event_id="event-1",
        analysis_input=analysis_input(),
        analysis_report=report(),
        policy_context=context(),
        suggested_deep_link="https://news.qq.com/rain/a/news-1",
        strategy_version="push-strategy-v1",
    )


def candidate(**overrides) -> PushStrategyCandidate:
    values = {
        "priority": PushPriority.B,
        "audience_rule": AudienceRule(interest_tags=("ai",)),
        "channels": (PushChannel.IN_APP,),
        "title": "AI 数据中心进入新阶段",
        "summary": "热点指标显示相关内容关注度持续增长。",
        "deep_link": "https://news.qq.com/rain/a/news-1",
        "send_time": NOW + timedelta(minutes=10),
        "expire_at": NOW + timedelta(hours=1),
        "frequency_limit": 1,
        "reason": "向关注 AI 的活跃用户提供热点信息。",
        "evidence_news_ids": ("evidence-1",),
    }
    values.update(overrides)
    return PushStrategyCandidate(**values)


def test_input_builder_only_exposes_allowed_business_options():
    value = strategy_input()

    assert value.news_id == "news-1"
    assert value.event_id == "event-1"
    assert value.audience_options.interest_tags == ("ai",)
    assert value.allowed_channels == (PushChannel.IN_APP,)
    assert value.allowed_evidence_news_ids == ("evidence-1",)
    assert "user_id" not in value.model_dump_json()
    assert "approved_plan_id" not in value.model_dump_json()


def test_input_builder_rejects_mismatched_analysis_identity():
    with pytest.raises(ValueError, match="analysis report"):
        PushStrategyInputBuilder().build(
            event_id="event-1",
            analysis_input=analysis_input(),
            analysis_report=report().model_copy(update={"news_id": "other"}),
            policy_context=context(),
            suggested_deep_link="https://news.qq.com/rain/a/news-1",
            strategy_version="push-strategy-v1",
        )


@pytest.mark.asyncio
async def test_runner_reuses_structured_fastgpt_client():
    expected = AgentResult(candidate(), "request-1", {"total_tokens": 20}, "{}")
    client = AsyncMock()
    client.run_structured.return_value = expected
    runner = PushStrategyAgentRunner(client, " push-app ")

    result = await runner.run(strategy_input())

    assert result is expected
    client.run_structured.assert_awaited_once_with(
        app_id="push-app",
        payload=strategy_input(),
        output_type=PushStrategyCandidate,
        mode="push_strategy_generation",
    )


def test_runner_requires_configured_app_id():
    with pytest.raises(ValueError, match="FASTGPT_PUSH_STRATEGY_APP_ID"):
        PushStrategyAgentRunner(AsyncMock(), None)


@pytest.mark.asyncio
async def test_service_builds_trusted_draft_and_runs_policy():
    plan_id = uuid4()
    analysis_run_id = uuid4()
    runner = AsyncMock()
    runner.run.return_value = AgentResult(candidate(), "request-1", {}, "{}")
    service = PushStrategyService(
        runner=runner,
        validator=PushPolicyValidator(),
    )

    execution = await service.generate(
        tenant_id="tenant-1",
        analysis_run_id=analysis_run_id,
        strategy_input=strategy_input(),
        policy_context=context(),
        plan_id=plan_id,
        now=NOW,
    )

    assert execution.plan.plan_id == plan_id
    assert execution.plan.analysis_run_id == analysis_run_id
    assert execution.plan.tenant_id == "tenant-1"
    assert execution.plan.strategy_version == "push-strategy-v1"
    assert execution.plan.requires_review is True
    assert execution.plan.status.value == "draft"
    assert execution.policy_result.decision == PushPolicyDecision.PASSED


@pytest.mark.asyncio
async def test_service_blocks_model_output_outside_trusted_scope():
    runner = AsyncMock()
    runner.run.return_value = AgentResult(
        candidate(
            audience_rule=AudienceRule(interest_tags=("finance",)),
            evidence_news_ids=("invented",),
        ),
        "request-2",
        {},
        "{}",
    )
    service = PushStrategyService(
        runner=runner,
        validator=PushPolicyValidator(),
    )

    execution = await service.generate(
        tenant_id="tenant-1",
        analysis_run_id=uuid4(),
        strategy_input=strategy_input(),
        policy_context=context(),
        now=NOW,
    )

    assert execution.policy_result.decision == PushPolicyDecision.BLOCKED
    assert {item.code for item in execution.policy_result.violations} >= {
        "unknown_evidence",
        "audience_interest_out_of_scope",
    }
