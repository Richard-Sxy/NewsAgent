from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import ApplicationError

from app.activities.news_steps import NewsStepActivities
from app.workflows.contracts import (
    HumanDecision,
    NewsWritingInput,
    StepCommand,
    StepOutcome,
)
from app.workflows.news_writing import NewsWritingWorkflow

""" 测试代码 """
@pytest.mark.asyncio
async def test_activity_delegates_to_business_handler() -> None:
    expected = StepOutcome(
        artifact_uri="s3://bucket/research.json",
        content_sha256="a" * 64,
        logical_key="research_package_v1",
    )
    handler = AsyncMock(return_value=expected)
    activities = NewsStepActivities(handler)
    command = StepCommand(
        tenant_id="tenant-1",
        job_id="job-1",
        step_type="research",
        step_key="research_package_v1",
    )

    assert await activities.run_news_step(command) == expected
    handler.assert_awaited_once_with(command)


def test_signal_ignores_decision_for_wrong_gate() -> None:
    instance = NewsWritingWorkflow()
    instance._waiting_gate = "outline"

    instance.submit_human_decision(
        HumanDecision(gate="research", action="approve")
    )
    assert instance._human_decision is None


def test_signal_accepts_current_gate_and_snapshot_is_small() -> None:
    instance = NewsWritingWorkflow()
    instance._waiting_gate = "final"
    decision = HumanDecision(gate="final", action="approve")
    instance.submit_human_decision(decision)

    snapshot = instance.snapshot()
    assert instance._human_decision == decision
    assert snapshot.waiting_gate == "final"
    assert snapshot.completed_steps == 0


def test_temporal_definitions_are_registered() -> None:
    assert NewsWritingWorkflow.__temporal_workflow_definition.name == "news-writing-v1"
    assert NewsStepActivities.run_news_step.__temporal_activity_definition.name == "run_news_step"
    assert NewsStepActivities.transition_job_state.__temporal_activity_definition.name == "transition_job_state"


@pytest.mark.asyncio
async def test_activity_marks_validation_failure_non_retryable() -> None:
    handler = AsyncMock(side_effect=ValueError("invalid payload"))
    activities = NewsStepActivities(handler)
    command = StepCommand("tenant", "job", "research", "research-v1")

    with pytest.raises(ApplicationError) as error:
        await activities.run_news_step(command)

    assert error.value.non_retryable is True


def outcome(key: str, **values) -> StepOutcome:
    return StepOutcome(
        artifact_uri=f"s3://bucket/{key}.json",
        content_sha256="a" * 64,
        logical_key=key,
        **values,
    )


@pytest.mark.asyncio
async def test_happy_path_reaches_final_approved() -> None:
    instance = NewsWritingWorkflow()
    instance._execute = AsyncMock(
        side_effect=[
            outcome("research"),
            outcome("outline", section_ids=["S01"]),
            outcome("section-S01"),
            outcome("draft"),
            outcome("review", decision="approve"),
            outcome("final"),
        ]
    )
    instance._wait_for_human = AsyncMock(
        side_effect=[
            HumanDecision(gate="research", action="approve"),
            HumanDecision(gate="outline", action="approve"),
            HumanDecision(gate="final", action="approve"),
        ]
    )

    result = await instance.run(
        NewsWritingInput(
            tenant_id="tenant-1",
            job_id="job-1",
            topic="主题",
            requirements={},
        )
    )

    assert result.status == "final_approved"
    assert result.final_artifact_uri == "s3://bucket/final.json"
    section_call = instance._execute.await_args_list[2]
    assert section_call.kwargs["inputs"]["progress"] == {
        "completed_sections": 1,
        "total_sections": 1,
    }


@pytest.mark.asyncio
async def test_research_package_stops_after_approved_research() -> None:
    instance = NewsWritingWorkflow()
    instance._execute = AsyncMock(return_value=outcome("research"))
    instance._wait_for_human = AsyncMock(
        return_value=HumanDecision(gate="research", action="approve")
    )
    instance._transition_state = AsyncMock()
    request = NewsWritingInput(
        tenant_id="tenant-1",
        job_id="job-1",
        topic="主题",
        requirements={},
        scenario="research_package",
    )

    result = await instance.run(request)

    assert result.status == "research_completed"
    assert result.research_artifact_uri == "s3://bucket/research.json"
    assert result.final_artifact_uri is None
    assert instance._execute.await_count == 1
    instance._transition_state.assert_awaited_once_with(
        request, "research_completed"
    )
    assert instance.snapshot().phase == "research_completed"


@pytest.mark.asyncio
async def test_review_loop_stops_after_three_rounds() -> None:
    instance = NewsWritingWorkflow()

    async def execute(_request, *, step_type, step_key, inputs, attempt=1):
        if step_type == "outline":
            return outcome(step_key, section_ids=["S01"])
        if step_type == "review":
            return outcome(
                step_key,
                decision="rewrite",
                rewrite_section_ids=["S01"],
            )
        return outcome(step_key)

    instance._execute = execute
    instance._transition_state = AsyncMock()
    instance._wait_for_human = AsyncMock(
        side_effect=[
            HumanDecision(gate="research", action="approve"),
            HumanDecision(gate="outline", action="approve"),
        ]
    )

    result = await instance.run(
        NewsWritingInput(
            tenant_id="tenant-1",
            job_id="job-1",
            topic="主题",
            requirements={},
        )
    )

    assert result.status == "waiting_human"
    assert result.reason == "审核轮次已达到上限 3"
    assert instance._review_round == 3
    instance._transition_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_supplemental_research_regenerates_downstream_artifacts() -> None:
    instance = NewsWritingWorkflow()
    instance._execute = AsyncMock(
        side_effect=[
            outcome("research-v2"),
            outcome("outline-v2", section_ids=["S01"]),
            outcome("section-v2"),
            outcome("draft-v2"),
            outcome("review-v2", decision="approve"),
            outcome("final-v2"),
        ]
    )
    instance._wait_for_human = AsyncMock(
        side_effect=[
            HumanDecision(gate="research", action="approve"),
            HumanDecision(gate="outline", action="approve"),
            HumanDecision(gate="final", action="approve"),
        ]
    )
    result = await instance.run(
        NewsWritingInput(
            tenant_id="tenant-1",
            job_id="job-1",
            topic="体育新闻",
            requirements={},
            recovery_action="research",
            recovery_instruction="补充一手来源",
        )
    )

    assert result.status == "final_approved"
    calls = instance._execute.await_args_list
    assert calls[0].kwargs["step_key"] == "research_package_v2"
    assert calls[0].kwargs["inputs"]["instruction"] == "补充一手来源"
    assert "previous_artifact" not in calls[0].kwargs["inputs"]
    assert calls[1].kwargs["step_key"] == "outline_research_v2"
    assert calls[2].kwargs["step_key"] == "section_S01_research_v2"
    assert calls[4].kwargs["step_key"] == "review_round_2"


@pytest.mark.asyncio
async def test_cancelled_gate_is_synchronized_to_database_activity() -> None:
    instance = NewsWritingWorkflow()
    instance._execute = AsyncMock(return_value=outcome("research"))
    instance._wait_for_human = AsyncMock(
        return_value=HumanDecision(gate="research", action="cancel")
    )
    instance._transition_state = AsyncMock()
    request = NewsWritingInput("tenant", "job", "topic", {})

    result = await instance.run(request)

    assert result.status == "cancelled"
    instance._transition_state.assert_awaited_once_with(request, "cancelled")


@pytest.mark.asyncio
async def test_pipeline_failure_is_synchronized_before_temporal_fails() -> None:
    instance = NewsWritingWorkflow()
    instance._execute = AsyncMock(side_effect=ValueError("invalid checkpoint"))
    instance._transition_state = AsyncMock()
    request = NewsWritingInput("tenant", "job", "topic", {})

    with pytest.raises(ValueError, match="invalid checkpoint"):
        await instance.run(request)

    instance._transition_state.assert_awaited_once_with(
        request,
        "failed",
        "ValueError: invalid checkpoint",
    )
    assert instance.snapshot().phase == "failed"
