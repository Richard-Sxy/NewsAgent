from unittest.mock import AsyncMock

import pytest
from temporalio.converter import DataConverter
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
@pytest.mark.parametrize("action", ["revise", "research"])
@pytest.mark.parametrize("scenario", ["assisted_writing", "research_package"])
async def test_supplemental_research_requires_explicit_approval(
    action: str, scenario: str,
) -> None:
    instance = NewsWritingWorkflow()

    async def execute(_request, *, step_type, step_key, inputs, attempt=1):
        assert step_type == "research", "unapproved research reached a downstream step"
        return outcome(step_key)

    instance._execute = AsyncMock(side_effect=execute)
    instance._wait_for_human = AsyncMock(
        return_value=HumanDecision(gate="research", action=action)
    )
    instance._transition_state = AsyncMock()
    request = NewsWritingInput(
        "tenant-1", "job-1", "主题", {},
        scenario=scenario, recovery_action="research",
    )

    result = await instance.run(request)

    assert result.status == "waiting_human"
    assert result.research_artifact_uri is None
    assert result.final_artifact_uri is None
    instance._execute.assert_awaited_once()
    instance._transition_state.assert_awaited_once_with(
        request, "waiting_human", "研究返工已达到上限"
    )


@pytest.mark.asyncio
async def test_approved_supplemental_research_package_can_complete() -> None:
    instance = NewsWritingWorkflow()
    instance._execute = AsyncMock(return_value=outcome("research-v2"))
    instance._wait_for_human = AsyncMock(
        return_value=HumanDecision(gate="research", action="approve")
    )
    instance._transition_state = AsyncMock()
    request = NewsWritingInput(
        "tenant-1", "job-1", "主题", {},
        scenario="research_package", recovery_action="research",
    )

    result = await instance.run(request)

    assert result.status == "research_completed"
    assert result.research_artifact_uri == "s3://bucket/research-v2.json"
    instance._execute.assert_awaited_once()
    instance._transition_state.assert_awaited_once_with(request, "research_completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revise", "research"])
@pytest.mark.parametrize("scenario", ["assisted_writing", "research_package"])
async def test_revised_research_still_requires_explicit_approval(
    action: str, scenario: str,
) -> None:
    instance = NewsWritingWorkflow()
    instance._execute = AsyncMock(
        side_effect=[outcome("research-v1"), outcome("research-v2")]
    )
    instance._wait_for_human = AsyncMock(side_effect=[
        HumanDecision(gate="research", action="revise"),
        HumanDecision(gate="research", action=action),
    ])
    instance._transition_state = AsyncMock()

    result = await instance.run(NewsWritingInput(
        "tenant-1", "job-1", "主题", {}, scenario=scenario,
    ))

    assert result.status == "waiting_human"
    assert instance._execute.await_count == 2
    assert {call.kwargs["step_type"] for call in instance._execute.await_args_list} == {
        "research"
    }


@pytest.mark.asyncio
async def test_research_recoveries_isolate_all_checkpoints_and_replay_the_same_branch(
    monkeypatch,
) -> None:
    # Existing checkpoints include the original pipeline's research/outline
    # revisions and second review. A fresh research branch must not read them.
    original_keys = {
        "research_package_v2", "outline_v2", "review_round_2",
        "draft_review_1_v1", "final_v1",
    }
    checkpoints = {
        key: outcome(f"old-{key}", section_ids=["S01"], decision="approve")
        for key in original_keys
    }
    activity_commands = []
    generated_keys = []

    async def execute_activity(_name, command, **_options):
        activity_commands.append(command)
        if command.step_key not in checkpoints:
            generated_keys.append(command.step_key)
            values = {}
            if command.step_type == "outline":
                values["section_ids"] = ["S01"]
            elif command.step_type == "review":
                values["decision"] = "approve"
            checkpoints[command.step_key] = outcome(command.step_key, **values)
        return checkpoints[command.step_key]

    monkeypatch.setattr(
        "app.workflows.news_writing.workflow.execute_activity", execute_activity,
    )

    async def run_branch(namespace: str):
        instance = NewsWritingWorkflow()
        instance._wait_for_human = AsyncMock(side_effect=[
            HumanDecision(gate="research", action="approve"),
            HumanDecision(gate="outline", action="revise", instruction="更新提纲"),
            HumanDecision(gate="outline", action="approve"),
            HumanDecision(gate="final", action="approve"),
        ])
        instance._transition_state = AsyncMock()
        request = NewsWritingInput(
            "tenant-1", "job-1", "主题", {}, recovery_action="research",
            recovery_checkpoint_namespace=namespace,
        )
        start = len(activity_commands)
        result = await instance.run(request)
        return result, activity_commands[start:]

    first, first_commands = await run_branch("a" * 64)
    second, second_commands = await run_branch("b" * 64)
    generation_count = len(generated_keys)
    replay, replay_commands = await run_branch("a" * 64)

    assert first.status == second.status == replay.status == "final_approved"
    first_keys = {command.step_key for command in first_commands}
    second_keys = {command.step_key for command in second_commands}
    assert first_keys.isdisjoint(original_keys)
    assert second_keys.isdisjoint(original_keys | first_keys)
    assert {command.step_key for command in replay_commands} == first_keys
    assert len(generated_keys) == generation_count
    assert first.final_artifact_uri != second.final_artifact_uri
    assert replay.final_artifact_uri == first.final_artifact_uri
    for commands in (first_commands, second_commands):
        research_ref = {
            "storage_uri": checkpoints[commands[0].step_key].artifact_uri,
            "content_sha256": "a" * 64,
        }
        for command in commands:
            if "research_artifact" in command.inputs:
                assert command.inputs["research_artifact"] == research_ref


def test_legacy_and_ordinary_recovery_checkpoint_keys_are_unchanged() -> None:
    for recovery_action in (None, "research"):
        legacy = NewsWritingInput(
            "tenant", "job", "topic", {}, recovery_action=recovery_action,
        )
        assert NewsWritingWorkflow._checkpoint_key(legacy, "outline_v2") == "outline_v2"


@pytest.mark.asyncio
async def test_legacy_temporal_input_without_namespace_still_decodes() -> None:
    converter = DataConverter.default
    payloads = await converter.encode([{
        "tenant_id": "tenant", "job_id": "job", "topic": "topic",
        "requirements": {}, "recovery_action": "research",
    }])

    decoded = await converter.decode(payloads, [NewsWritingInput])

    assert decoded[0].recovery_checkpoint_namespace is None
    assert NewsWritingWorkflow._checkpoint_key(decoded[0], "outline_v2") == "outline_v2"


def test_namespaced_checkpoint_key_fits_the_persisted_idempotency_field() -> None:
    request = NewsWritingInput(
        "tenant", "job", "topic", {}, recovery_action="research",
        recovery_checkpoint_namespace="a" * 64,
    )
    long_key = f"section_S{'1' * 150}_review_2_v2"
    key = NewsWritingWorkflow._checkpoint_key(request, long_key)
    assert key != NewsWritingWorkflow._checkpoint_key(request, long_key + "1")
    assert len(f"{key}:2") <= 160


@pytest.mark.parametrize("namespace", ["", "a" * 63, "g" * 64])
def test_invalid_checkpoint_namespace_is_rejected(namespace: str) -> None:
    request = NewsWritingInput(
        "tenant", "job", "topic", {}, recovery_action="research",
        recovery_checkpoint_namespace=namespace,
    )
    with pytest.raises(ValueError, match="invalid research recovery"):
        NewsWritingWorkflow._checkpoint_key(request, "research_package_v2")


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
