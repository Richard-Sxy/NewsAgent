from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.clients.fastgpt import AgentResult
from app.domain.errors import AgentOutputValidationError
from app.schemas.error_attribution import (
    CaseErrorAttribution,
    ErrorAttributionInput,
    ErrorAttributionReport,
)
from app.schemas.evaluation_dataset import (
    EvaluationExpectedLabel,
    EvaluationSourceLineage,
    FrozenEvaluationCase,
)
from app.schemas.hot_news import (
    HotNewsAnalysisInput,
    HotNewsMetrics,
    HotScoreComponents,
    RelatedNewsEvidence,
)
from app.services.agents.error_attribution import (
    ErrorAttributionAgentRunner,
)


CASE_ID = UUID("00000000-0000-0000-0000-000000000001")
DATASET_ID = UUID("20000000-0000-0000-0000-000000000001")


def attribution_input() -> ErrorAttributionInput:
    analysis_input = HotNewsAnalysisInput(
        news_id="news-1",
        title="当前新闻",
        content_type="article",
        window_start=datetime(2026, 9, 8, tzinfo=UTC),
        window_end=datetime(2026, 9, 9, tzinfo=UTC),
        hot_score=0.8,
        metrics=HotNewsMetrics(
            impressions=100,
            clicks=20,
            ctr=0.2,
            unique_users=18,
            effective_consumptions=9,
            interactions=4,
        ),
        score_components=HotScoreComponents(
            click=0.7,
            consumption=0.4,
            interaction=0.2,
            growth=0.8,
        ),
        related_news=[
            RelatedNewsEvidence(
                news_id="evidence-1",
                title="允许引用的新闻",
                final_score=0.8,
            )
        ],
        analysis_policy_version="prompt-v1",
    )
    case = FrozenEvaluationCase(
        case_id=f"feedback:{CASE_ID}",
        feedback_case_id=CASE_ID,
        news_id="news-1",
        layer="fresh_bad_case",
        severity="high",
        analysis_input=analysis_input,
        observed_output={"news_id": "news-1"},
        expected=EvaluationExpectedLabel(
            verdict="incorrect",
            allowed_dominant_drivers=("click",),
            operator_comment="归因仅用于生成候选建议。",
        ),
        lineage=EvaluationSourceLineage(
            feedback_case_id=CASE_ID,
            feedback_content_sha256="a" * 64,
            run_id=None,
            run_idempotency_key="run-1",
            source_type="operator_corrected",
            problem_type="analysis_incorrect",
            source_reference={
                "reference_type": "operator_decision",
                "reference_id": "decision-1",
            },
            production_bundle_version="bundle-v1",
            occurred_at=datetime(2026, 9, 8, 12, tzinfo=UTC),
            recorded_at=datetime(2026, 9, 8, 13, tzinfo=UTC),
            label_id=UUID("10000000-0000-0000-0000-000000000001"),
            label_version=1,
            label_approved_by="reviewer-1",
            label_approved_at=datetime(2026, 9, 8, 14, tzinfo=UTC),
        ),
    )
    return ErrorAttributionInput(
        dataset_id=DATASET_ID,
        dataset_sha256="b" * 64,
        cases=(case,),
    )


def report(**overrides) -> ErrorAttributionReport:
    values = {
        "dataset_id": DATASET_ID,
        "dataset_sha256": "b" * 64,
        "attributions": (
            CaseErrorAttribution(
                case_id=f"feedback:{CASE_ID}",
                category="prompt_instruction",
                evidence_news_ids=("evidence-1",),
                rationale="输出没有按标签区分主要驱动因素。",
                affected_asset="analysis_prompt",
                remediation_suggestion="生成 Prompt 候选 Diff 并离线评测。",
                confidence=0.8,
            ),
        ),
    }
    values.update(overrides)
    return ErrorAttributionReport(**values)


@pytest.mark.asyncio
async def test_runner_reuses_fastgpt_structured_call_and_validates_refs():
    input_value = attribution_input()
    output = report()
    client = AsyncMock()
    client.run_structured.return_value = AgentResult(
        value=output,
        request_id="request-1",
        usage={"total_tokens": 20},
        raw_content=output.model_dump_json(),
    )
    runner = ErrorAttributionAgentRunner(client, " attribution-app ")

    result = await runner.run(input_value)

    assert result.value == output
    client.run_structured.assert_awaited_once_with(
        app_id="attribution-app",
        payload=input_value,
        output_type=ErrorAttributionReport,
        mode="hot_news_error_attribution",
    )


@pytest.mark.asyncio
async def test_runner_rejects_unknown_evidence_reference():
    input_value = attribution_input()
    output = report(
        attributions=(
            CaseErrorAttribution(
                case_id=f"feedback:{CASE_ID}",
                category="retrieval_miss",
                evidence_news_ids=("invented-evidence",),
                rationale="召回缺失。",
                affected_asset="retrieval_policy",
                remediation_suggestion="扩大候选检索召回。",
                confidence=0.7,
            ),
        )
    )
    client = AsyncMock()
    client.run_structured.return_value = AgentResult(
        value=output,
        request_id="request-2",
        usage={},
        raw_content=output.model_dump_json(),
    )

    with pytest.raises(AgentOutputValidationError, match="unknown evidence"):
        await ErrorAttributionAgentRunner(client, "app").run(input_value)


def test_output_contract_forbids_unknown_taxonomy_and_authoritative_metrics():
    payload = report().model_dump(mode="json")
    payload["attributions"][0]["category"] = "train_the_model"
    payload["authoritative_metrics"] = {"pass_rate": 0.99}

    with pytest.raises(ValidationError):
        ErrorAttributionReport.model_validate(payload)
