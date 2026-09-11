"""Opt-in Temporal test for the real 48-hour Data Loop approval timer."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from time import perf_counter
from uuid import uuid4

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.activities.data_loop import DataLoopActivities
from app.workflows.data_loop import (
    DATA_LOOP_APPROVAL_TIMEOUT,
    HotNewsDataLoopWorkflow,
)
from app.workflows.data_loop_contracts import (
    DataLoopRunRequest,
    DataLoopStepCommand,
    DataLoopStepOutcome,
)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_TEMPORAL_TIME_SKIPPING") != "1",
        reason="set RUN_TEMPORAL_TIME_SKIPPING=1 to start Temporal test server",
    ),
]


class _RecordingStepHandler:
    def __init__(self) -> None:
        self.commands: list[DataLoopStepCommand] = []

    async def execute(self, command: DataLoopStepCommand) -> DataLoopStepOutcome:
        self.commands.append(command)
        values_by_step = {
            "freeze_dataset": {"dataset_id": "dataset-time-skipping"},
            "attribute_errors": {"attribution_count": 1},
            "evaluate_candidate": {
                "evaluation_run_id": "evaluation-time-skipping",
                "gate_passed": True,
            },
            "record_promotion_decision": {"decision_id": "decision-timeout"},
        }
        if command.step_type == "activate_bundle":
            raise AssertionError("an expired approval must never activate a bundle")
        return DataLoopStepOutcome(
            step_key=command.step_key,
            status="completed",
            values=values_by_step[command.step_type],
        )


def _request() -> DataLoopRunRequest:
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    return DataLoopRunRequest(
        tenant_id="tenant-time-skipping",
        window_start=start,
        window_end=start + timedelta(days=1),
        dataset_name="daily-feedback",
        dataset_version="2026-09-10.time-skipping",
        golden_dataset_id="11111111-1111-4111-8111-111111111111",
        high_risk_regression_dataset_id=(
            "22222222-2222-4222-8222-222222222222"
        ),
        candidate_id="candidate-time-skipping",
        previous_experiment_candidate_id=None,
        evaluation_policy_version="hot-news-gate-v1",
        idempotency_key=f"time-skipping-{uuid4()}",
    )


@pytest.mark.asyncio
async def test_real_48_hour_timer_records_system_rejection_without_activation(
) -> None:
    """Run the production Workflow against Temporal's time-skipping server."""

    handler = _RecordingStepHandler()
    activity = DataLoopActivities(handler)
    server_kwargs: dict[str, str] = {}
    if existing_path := os.getenv("TEMPORAL_TEST_SERVER_PATH"):
        server_kwargs["test_server_existing_path"] = existing_path
    if download_dir := os.getenv("TEMPORAL_TEST_SERVER_DOWNLOAD_DIR"):
        server_kwargs["download_dest_dir"] = download_dir

    async with await WorkflowEnvironment.start_time_skipping(
        **server_kwargs,
    ) as environment:
        assert environment.supports_time_skipping is True
        task_queue = f"data-loop-time-skipping-{uuid4()}"
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[HotNewsDataLoopWorkflow],
            activities=[activity.run_data_loop_step],
        ):
            request = _request()
            started_at = await environment.get_current_time()
            handle = await environment.client.start_workflow(
                HotNewsDataLoopWorkflow.run,
                request,
                id=f"data-loop-time-skipping-{uuid4()}",
                task_queue=task_queue,
            )
            wall_started_at = perf_counter()
            async with asyncio.timeout(30):
                result = await handle.result()
            wall_elapsed = perf_counter() - wall_started_at
            finished_at = await environment.get_current_time()
            history = await handle.fetch_history()

    assert finished_at - started_at >= DATA_LOOP_APPROVAL_TIMEOUT
    assert result.status == "approval_expired"
    assert result.production_bundle_id is None
    assert result.reason == "approval timeout; candidate was not activated"
    assert [command.step_type for command in handler.commands] == [
        "freeze_dataset",
        "attribute_errors",
        "evaluate_candidate",
        "record_promotion_decision",
    ]
    timeout_decision = handler.commands[-1]
    assert timeout_decision.inputs["action"] == "reject"
    assert timeout_decision.inputs["actor_id"] == "system"
    assert timeout_decision.inputs["reason"] == (
        "approval timeout; default is no promotion"
    )
    assert timeout_decision.inputs["idempotency_key"] == (
        f"{request.idempotency_key}:approval-timeout"
    )

    timer_started = [
        event
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_TIMER_STARTED
    ]
    timer_fired = [
        event
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_TIMER_FIRED
    ]
    assert [
        event.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
        for event in timer_started
    ] == [DATA_LOOP_APPROVAL_TIMEOUT]
    assert len(timer_fired) == 1
    assert (
        timer_fired[0].timer_fired_event_attributes.started_event_id
        == timer_started[0].event_id
    )
    assert wall_elapsed < 30
