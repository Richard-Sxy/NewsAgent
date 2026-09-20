"""推送候选计划的输入构建与统一生成入口。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from app.clients.fastgpt import AgentResult
from app.schemas.hot_news import HotNewsAnalysisInput, HotNewsAnalysisReport
from app.schemas.push import (
    PushAudienceOptions,
    PushPlan,
    PushPlanStatus,
    PushPolicyContext,
    PushPolicyResult,
    PushStrategyCandidate,
    PushStrategyInput,
)
from app.services.agents.push_strategy import PushStrategyAgentRunner
from app.services.push_policy import PushPolicyValidator


class PushStrategyInputBuilder:
    """从可信热点快照构造不含用户明细的模型输入。"""

    def build(
        self,
        *,
        event_id: str,
        analysis_input: HotNewsAnalysisInput,
        analysis_report: HotNewsAnalysisReport,
        policy_context: PushPolicyContext,
        suggested_deep_link: str,
        strategy_version: str,
    ) -> PushStrategyInput:
        if analysis_report.news_id != analysis_input.news_id:
            raise ValueError("analysis report does not match analysis input")
        if policy_context.news_id != analysis_input.news_id:
            raise ValueError("policy context news_id does not match analysis input")
        if policy_context.event_id != event_id:
            raise ValueError("policy context event_id does not match event_id")
        if policy_context.earliest_send_time is None:
            raise ValueError("policy context requires earliest_send_time")
        if policy_context.latest_expire_at is None:
            raise ValueError("policy context requires latest_expire_at")
        if not any(
            suggested_deep_link.startswith(prefix)
            for prefix in policy_context.allowed_deep_link_prefixes
        ):
            raise ValueError("suggested_deep_link is outside trusted prefixes")

        return PushStrategyInput(
            news_id=analysis_input.news_id,
            event_id=event_id,
            title=analysis_input.title,
            summary=analysis_input.summary,
            hot_score=analysis_input.hot_score,
            metrics=analysis_input.metrics,
            analysis=analysis_report,
            audience_options=PushAudienceOptions(
                allow_all_users=policy_context.allow_all_users,
                region_codes=tuple(sorted(policy_context.allowed_region_codes)),
                interest_tags=tuple(sorted(policy_context.allowed_interest_tags)),
                subscription_topics=tuple(
                    sorted(policy_context.allowed_subscription_topics)
                ),
                user_segments=tuple(sorted(policy_context.allowed_user_segments)),
            ),
            allowed_channels=tuple(
                sorted(policy_context.allowed_channels, key=lambda item: item.value)
            ),
            allowed_evidence_news_ids=tuple(
                sorted(policy_context.allowed_evidence_news_ids)
            ),
            suggested_deep_link=suggested_deep_link,
            earliest_send_time=policy_context.earliest_send_time,
            expire_by=policy_context.latest_expire_at,
            strategy_version=strategy_version,
        )


@dataclass(frozen=True, slots=True)
class PushStrategyExecution:
    strategy_input: PushStrategyInput
    candidate: AgentResult[PushStrategyCandidate]
    plan: PushPlan
    policy_result: PushPolicyResult


class PushStrategyService:
    """调用模型生成候选计划，并立即执行确定性策略校验。"""

    def __init__(
        self,
        *,
        runner: PushStrategyAgentRunner,
        validator: PushPolicyValidator,
    ) -> None:
        self._runner = runner
        self._validator = validator

    async def generate(
        self,
        *,
        tenant_id: str,
        analysis_run_id: UUID,
        strategy_input: PushStrategyInput,
        policy_context: PushPolicyContext,
        plan_id: UUID | None = None,
        now: datetime | None = None,
        duplicate_detected: bool = False,
    ) -> PushStrategyExecution:
        if tenant_id != policy_context.tenant_id:
            raise ValueError("tenant_id does not match policy context")
        if strategy_input.news_id != policy_context.news_id:
            raise ValueError("strategy input news_id does not match policy context")
        if strategy_input.event_id != policy_context.event_id:
            raise ValueError("strategy input event_id does not match policy context")
        candidate_result = await self._runner.run(strategy_input)
        candidate = candidate_result.value
        plan = PushPlan(
            plan_id=plan_id or uuid4(),
            analysis_run_id=analysis_run_id,
            tenant_id=tenant_id,
            news_id=strategy_input.news_id,
            event_id=strategy_input.event_id,
            priority=candidate.priority,
            audience_rule=candidate.audience_rule,
            channels=candidate.channels,
            title=candidate.title,
            summary=candidate.summary,
            deep_link=candidate.deep_link,
            send_time=candidate.send_time,
            expire_at=candidate.expire_at,
            frequency_limit=candidate.frequency_limit,
            requires_review=True,
            reason=candidate.reason,
            evidence_news_ids=candidate.evidence_news_ids,
            strategy_version=strategy_input.strategy_version,
            status=PushPlanStatus.DRAFT,
        )
        policy_result = self._validator.validate(
            plan=plan,
            context=policy_context,
            now=now,
            duplicate_detected=duplicate_detected,
        )
        return PushStrategyExecution(
            strategy_input=strategy_input,
            candidate=candidate_result,
            plan=plan,
            policy_result=policy_result,
        )
