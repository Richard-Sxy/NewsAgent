"""Opt-in real Temporal contract; its own server/history/queue are disposable."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from opentelemetry import baggage, context
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from temporalio.converter import PayloadConverter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker, Replayer

from app.observability import telemetry
from app.observability.settings import ObservabilitySettings
from app.observability.temporal import SafeTracingInterceptor
from tests.observability_temporal_workflow import ObservabilityContractWorkflow, observability_contract_step


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("NEWS_AGENT_OBSERVABILITY_TEMPORAL_TEST") != "1",
    reason="Opt in to an isolated local Temporal dev server; downloads official CLI if absent",
)]


@pytest.mark.asyncio
async def test_real_temporal_trace_survives_workflow_activity_retry_and_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    metrics = InMemoryMetricReader()
    runtime = telemetry.configure_telemetry(settings=ObservabilitySettings(enabled=True, json_logs=False),
        span_exporter=spans, metric_reader=metrics, log_exporter=logs)
    cache = Path(os.environ.get("NEWS_AGENT_TEMPORAL_CLI_CACHE", str(tmp_path / "temporal-cli")))
    cache.mkdir(parents=True, exist_ok=True)
    task_queue = f"observability-contract-{uuid4()}"
    history = None
    try:
        async with asyncio.timeout(75):
            async with await WorkflowEnvironment.start_local(ui=False, download_dest_dir=str(cache),
                    interceptors=[SafeTracingInterceptor()]) as env:
                async with Worker(env.client, task_queue=task_queue,
                        workflows=[ObservabilityContractWorkflow], activities=[observability_contract_step]):
                    token = context.attach(baggage.set_baggage("user-identity", "SECRET_IDENTITY"))
                    try:
                        with telemetry.operation("diagnostic.api.submit") as parent:
                            handle = await env.client.start_workflow(ObservabilityContractWorkflow.run,
                                "SECRET_NEWS_INPUT", id=f"observability-contract-{uuid4()}",
                                task_queue=task_queue)
                            assert await handle.result() == {"attempt": 2, "result": "completed"}
                    finally:
                        context.detach(token)
                    history = await handle.fetch_history()

        exported = spans.get_finished_spans()
        attempts = [span for span in exported if span.name == "temporal.activity"]
        run_activities = [span for span in exported if span.name == "RunActivity:observability_contract_step_v1"]
        model_calls = [span for span in exported if span.name == "diagnostic.model.boundary"]
        assert len(attempts) == len(run_activities) == len(model_calls) == 2
        assert {span.attributes["temporal.activity.attempt"] for span in attempts} == {1, 2}
        trace_id = parent.get_span_context().trace_id
        assert all(span.context.trace_id == trace_id for span in exported)
        assert {span.parent.span_id for span in attempts} == {span.context.span_id for span in run_activities}
        assert {span.parent.span_id for span in model_calls} == {span.context.span_id for span in attempts}
        assert [span for span in attempts if span.attributes["temporal.activity.attempt"] == 1][0].status.status_code is StatusCode.ERROR
        assert [span for span in attempts if span.attributes["temporal.activity.attempt"] == 2][0].status.status_code is not StatusCode.ERROR
        assert any(span.name.startswith("RunWorkflow:") for span in exported)
        for event in history.events:
            for field in ("workflow_execution_started_event_attributes", "activity_task_scheduled_event_attributes"):
                if event.HasField(field):
                    headers = getattr(event, field).header.fields
                    carrier = PayloadConverter.default.from_payloads([headers["_tracer-data"]])[0]
                    assert "traceparent" in carrier and "baggage" not in carrier

        before_replay = len(exported)
        await Replayer(workflows=[ObservabilityContractWorkflow],
                       interceptors=[SafeTracingInterceptor()]).replay_workflow(history)
        replayed = spans.get_finished_spans()[before_replay:]
        # Official Temporal interceptor may emit an explicit Complete/Replay marker;
        # replay must never repeat activities or the real model operations.
        assert not any(span.name in {"temporal.activity", "diagnostic.model.boundary"} or
                       span.name.startswith("RunActivity:") for span in replayed)
        serialized = json.dumps([json.loads(span.to_json()) for span in spans.get_finished_spans()])
        serialized += str(logs.get_finished_logs())
        assert "SECRET_" not in serialized
        data = metrics.get_metrics_data()
        attempt_points = [point for resource in data.resource_metrics for scope in resource.scope_metrics
            for metric in scope.metrics if metric.name == "newsagent.operation.calls"
            for point in metric.data.data_points if point.attributes.get("newsagent.operation") == "temporal.activity"]
        assert sum(point.value for point in attempt_points) == 2
        assert {point.attributes["newsagent.outcome"] for point in attempt_points} == {"success", "error"}
    finally:
        runtime.shutdown()
