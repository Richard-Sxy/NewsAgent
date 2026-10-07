"""Opt-in replay of real pre-patch and analysis-patched Temporal histories.

Only a dedicated local test Temporal address is accepted. Activities use fixed
in-memory fixtures; these tests never load Settings, a model, or a business DB.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest
from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

with workflow.unsafe.imports_passed_through():
    from app.activities.data_loop import DataLoopActivities
    from app.workflows.data_loop import DATA_LOOP_WORKFLOW_NAME, HotNewsDataLoopWorkflow
    from app.workflows.data_loop_contracts import (
        DataLoopRunRequest,
        DataLoopStepCommand,
        DataLoopStepOutcome,
        DataLoopWorkflowResult,
    )


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_TEMPORAL_ANALYSIS_REPLAY") != "1",
        reason="set RUN_TEMPORAL_ANALYSIS_REPLAY=1 with a dedicated local test Temporal",
    ),
]


@workflow.defn(name=DATA_LOOP_WORKFLOW_NAME, sandboxed=False)
class _LegacyDataLoopWorkflow(HotNewsDataLoopWorkflow):
    """Minimal actual pre-patch three-command path, with no patched() call.

    The inherited _execute still has the original Activity type, timeouts,
    heartbeat, and RetryPolicy. A failed gate ends this old path before timers
    or human approval. Current production code is replayed in its normal sandbox.
    """

    @workflow.run
    async def run(self, request: DataLoopRunRequest) -> DataLoopWorkflowResult:
        request.validate()
        self._tenant_id = request.tenant_id
        self._candidate_id = request.candidate_id
        frozen = await self._execute(
            request,
            step_type="freeze_dataset",
            step_key="freeze-dataset-v1",
            inputs={
                "window_start": request.window_start.isoformat(),
                "window_end": request.window_end.isoformat(),
                "dataset_name": request.dataset_name,
                "dataset_version": request.dataset_version,
            },
        )
        self._dataset_id = self._required_string(frozen, "dataset_id")
        attributed = await self._execute(
            request,
            step_type="attribute_errors",
            step_key="attribute-errors-v1",
            inputs={"dataset_id": self._dataset_id},
        )
        evaluated = await self._execute(
            request,
            step_type="evaluate_candidate",
            step_key="evaluate-candidate-v1",
            inputs={
                "dataset_id": self._dataset_id,
                "golden_dataset_id": request.golden_dataset_id,
                "high_risk_regression_dataset_id": request.high_risk_regression_dataset_id,
                "candidate_id": request.candidate_id,
                "previous_experiment_candidate_id": request.previous_experiment_candidate_id,
                "evaluation_policy_version": request.evaluation_policy_version,
                "attribution_status": "completed",
                "attribution_artifact_uri": attributed.values.get("artifact_uri"),
                "attribution_artifact_sha256": attributed.values.get("artifact_sha256"),
            },
        )
        self._evaluation_run_id = self._required_string(evaluated, "evaluation_run_id")
        self._gate_passed = self._required_bool(evaluated, "gate_passed")
        if self._gate_passed:
            raise AssertionError("legacy fixture must stop at a failed gate")
        self._phase = "evaluation_failed"
        return DataLoopWorkflowResult(
            status="evaluation_failed",
            candidate_id=request.candidate_id,
            dataset_id=self._dataset_id,
            evaluation_run_id=self._evaluation_run_id,
            reason=str(evaluated.values.get("reason") or "evaluation gate failed"),
        )


class _FixedStepHandler:
    def __init__(self):
        self.commands: list[DataLoopStepCommand] = []

    async def execute(self, command: DataLoopStepCommand) -> DataLoopStepOutcome:
        self.commands.append(command)
        values = {
            "freeze_dataset": {"dataset_id": "dataset-replay"},
            "analyze_dataset": {
                "analysis_status": "completed",
                "artifact_uri": "s3://replay-fixture/dataset-analysis.json",
                "artifact_sha256": "b" * 64,
                "summary": {
                    "case_count": 1, "source_run_count": 1, "analyzed_run_count": 1,
                    "covered_case_count": 1, "unavailable_case_count": 0,
                },
            },
            "attribute_errors": {"attribution_count": 1},
            "evaluate_candidate": {
                "evaluation_run_id": "evaluation-replay",
                "gate_passed": False,
                "reason": "fixed failed gate",
            },
        }[command.step_type]
        return DataLoopStepOutcome(step_key=command.step_key, status="completed", values=values)


def _request() -> DataLoopRunRequest:
    start = datetime(2026, 10, 6, tzinfo=timezone.utc)
    return DataLoopRunRequest(
        tenant_id="tenant-data-loop-history-replay",
        window_start=start,
        window_end=start + timedelta(days=1),
        dataset_name="data-loop-replay-fixture",
        dataset_version="2026-10-07.v1",
        golden_dataset_id="11111111-1111-4111-8111-111111111111",
        high_risk_regression_dataset_id="22222222-2222-4222-8222-222222222222",
        candidate_id=str(uuid4()),
        previous_experiment_candidate_id=None,
        evaluation_policy_version="hot-news-gate-v1",
        idempotency_key=f"replay-fixture-{uuid4()}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [True, False], ids=["pre-patch-history", "patched-history"])
async def test_current_data_loop_replays_real_history(legacy: bool) -> None:
    address = os.getenv("TEMPORAL_ANALYSIS_TEST_ADDRESS", "")
    # Do not infer a production address/namespace from application settings.
    assert address in {"127.0.0.1:17233", "localhost:17233"}, (
        "TEMPORAL_ANALYSIS_TEST_ADDRESS must explicitly select the local test server on 17233"
    )
    handler = _FixedStepHandler()
    activities = DataLoopActivities(handler)
    definition = _LegacyDataLoopWorkflow if legacy else HotNewsDataLoopWorkflow
    async with asyncio.timeout(45):
        client = await Client.connect(address, namespace="default")
        async with Worker(
            client,
            task_queue=f"data-loop-history-replay-{uuid4()}",
            workflows=[definition],
            activities=[activities.run_data_loop_step],
        ) as worker:
            handle = await client.start_workflow(
                definition.run,
                _request(),
                id=f"data-loop-history-replay-{uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            result = await handle.result()
            history = await handle.fetch_history()

        assert result.status == "evaluation_failed"
        expected_steps = ["freeze_dataset", "attribute_errors", "evaluate_candidate"]
        if not legacy:
            expected_steps.insert(1, "analyze_dataset")
        assert [command.step_type for command in handler.commands] == expected_steps
        scheduled = [
            event for event in history.events
            if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        ]
        assert len(scheduled) == len(expected_steps)
        patch_markers = [
            event for event in history.events
            if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
            and event.marker_recorded_event_attributes.marker_name == "core_patch"
        ]
        assert bool(patch_markers) is (not legacy)
        evaluation = handler.commands[-1].inputs
        if legacy:
            assert not any(key.startswith("dataset_analysis_") for key in evaluation)
        else:
            assert evaluation["dataset_analysis_status"] == "completed"
            assert evaluation["dataset_analysis_artifact_sha256"] == "b" * 64

        replay = await Replayer(workflows=[HotNewsDataLoopWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None
