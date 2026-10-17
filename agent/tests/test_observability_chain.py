"""Real SDK exporters verify propagation, privacy and domain error semantics."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from sqlalchemy import create_engine, text

from app.observability import telemetry, web, database, temporal
from app.observability.decorators import traced
from app.observability.web import TelemetryMiddleware


@pytest.fixture
def capture(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    metrics = MeterProvider(metric_readers=[reader])
    tracer = provider.get_tracer("chain-test")
    meter = metrics.get_meter("chain-test")
    for module in (telemetry, web, database, temporal):
        monkeypatch.setattr(module, "get_tracer", lambda: tracer)
    monkeypatch.setattr(telemetry, "get_meter", lambda: meter)
    monkeypatch.setattr(web, "get_meter", lambda: meter)
    yield exporter, reader, tracer
    provider.shutdown()
    metrics.shutdown()


@pytest.mark.asyncio
async def test_http_routes_streams_concurrency_and_traceparent(capture):
    exporter, reader, tracer = capture
    app = FastAPI()
    app.add_middleware(TelemetryMiddleware)

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        with telemetry.operation("tool.test"):
            await asyncio.sleep(0)
            return {"ok": True}

    parents = ["00-" + f"{i:032x}" + "-1234567890abcdef-01" for i in (11, 22)]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        replies = await asyncio.gather(*[
            client.get("/items/secret-user-id?token=SECRET", headers={"traceparent": parent,
                "authorization": "Bearer SECRET", "baggage": "secret=SECRET"}) for parent in parents])
    assert [response.headers["x-trace-id"] for response in replies] == [f"{i:032x}" for i in (11, 22)]
    spans = exporter.get_finished_spans()
    servers = [span for span in spans if span.name == "GET /items/{item_id}"]
    children = [span for span in spans if span.name == "tool.test"]
    assert len(servers) == len(children) == 2
    assert {child.parent.span_id for child in children} == {server.context.span_id for server in servers}
    assert "SECRET" not in str(spans)
    points = [point for resource in reader.get_metrics_data().resource_metrics
              for scope in resource.scope_metrics for metric in scope.metrics
              for point in metric.data.data_points]
    assert all("secret-user-id" not in str(point.attributes) for point in points)


@pytest.mark.asyncio
async def test_http_sse_span_ends_after_last_chunk_and_errors_are_private(capture):
    from fastapi.responses import StreamingResponse
    exporter, _, tracer = capture
    app = FastAPI()
    app.add_middleware(TelemetryMiddleware)

    @app.get("/stream")
    async def stream():
        async def chunks():
            yield b"data: one\n\n"
            assert not [s for s in exporter.get_finished_spans() if s.name == "GET /stream"]
            with telemetry.operation("stream.last_chunk"):
                yield b"data: two\n\n"
        return StreamingResponse(chunks(), media_type="text/event-stream")

    @app.get("/fail")
    async def fail():
        raise RuntimeError("SECRET model output")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        assert "two" in (await client.get("/stream")).text
        assert (await client.get("/fail")).status_code == 500
    spans = exporter.get_finished_spans()
    assert [s for s in spans if s.name == "GET /fail"][0].status.status_code == StatusCode.ERROR
    serialized = json.dumps([json.loads(s.to_json()) for s in spans])
    assert "SECRET" not in serialized


def test_temporal_official_header_propagation_filters_baggage_and_errors(capture):
    from opentelemetry import baggage
    from opentelemetry.context import attach, detach
    exporter, _, tracer = capture
    interceptor = temporal.SafeTracingInterceptor()
    token = attach(baggage.set_baggage("secret", "SECRET"))
    try:
        with tracer.start_as_current_span("api") as parent:
            headers = interceptor._context_to_headers({})
            restored = interceptor._context_from_headers(headers)
            assert trace.get_current_span(restored).get_span_context().trace_id == parent.get_span_context().trace_id
            carrier = interceptor.payload_converter.from_payloads([headers[interceptor.header_key]])[0]
            assert "baggage" not in carrier
            with pytest.raises(ValueError):
                with interceptor.tracer.start_as_current_span("activity", context=restored):
                    raise ValueError("SECRET prompt and password")
    finally:
        detach(token)
    spans = exporter.get_finished_spans()
    child = next(s for s in spans if s.name == "activity")
    assert child.status.status_code == StatusCode.ERROR
    assert "SECRET" not in child.to_json()


@pytest.mark.asyncio
async def test_temporal_activity_attempt_span_is_created_after_remote_parent_restoration(capture, monkeypatch):
    from temporalio.worker import ActivityInboundInterceptor
    exporter, _, tracer = capture
    interceptor = temporal.SafeTracingInterceptor()
    monkeypatch.setattr(temporal.activity, "info", lambda: SimpleNamespace(
        activity_id="activity-1", activity_type="diagnostic", attempt=2,
        workflow_id="workflow-1", workflow_run_id="workflow-run-1",
    ))

    class Execute(ActivityInboundInterceptor):
        async def execute_activity(self, input):
            with telemetry.operation("activity.business"):
                return "completed"

    with tracer.start_as_current_span("api.submit") as api:
        headers = interceptor._context_to_headers({})
    # Worker task execution has no local API context; only the durable headers.
    assert await interceptor.intercept_activity(Execute(None)).execute_activity(
        SimpleNamespace(headers=headers, args=())
    ) == "completed"
    spans = {span.name: span for span in exporter.get_finished_spans()}
    server = spans["RunActivity:diagnostic"]
    attempt = spans["temporal.activity"]
    assert attempt.context.trace_id == api.get_span_context().trace_id
    assert attempt.parent.span_id == server.context.span_id
    assert server.parent.span_id == api.get_span_context().span_id
    assert spans["activity.business"].parent.span_id == attempt.context.span_id
    assert attempt.attributes["temporal.activity.attempt"] == 2


def test_database_success_and_failure_do_not_capture_sql_or_values(capture):
    exporter, _, tracer = capture
    engine = create_engine("sqlite://")
    database.instrument_engine(engine)
    with tracer.start_as_current_span("request") as parent, engine.connect() as connection:
        connection.execute(text("SELECT :credential"), {"credential": "SECRET"})
        with pytest.raises(Exception):
            connection.execute(text("SELECT SECRET FROM missing_secret_table"))
    spans = [s for s in exporter.get_finished_spans() if s.name == "db.query"]
    assert len(spans) == 2
    assert all(s.parent.span_id == parent.get_span_context().span_id for s in spans)
    assert spans[1].status.status_code == StatusCode.ERROR
    assert "SECRET" not in "".join(s.to_json() for s in spans)
    engine.dispose()


@pytest.mark.asyncio
async def test_swallowed_domain_failure_still_fails_span(capture):
    exporter, _, _ = capture
    @traced("conversation.turn", result_status=True)
    async def turn():
        return SimpleNamespace(status="failed", assistant_content="SECRET")
    await turn()
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert "SECRET" not in span.to_json()


@pytest.mark.asyncio
async def test_query_and_confirmed_hook_denials_are_visible_without_exporting_arguments(capture):
    from app.conversation.service import ConversationAgentService
    from app.conversation.tools import ConversationToolDenied
    exporter, _, _ = capture
    service = object.__new__(ConversationAgentService)
    service._tools = SimpleNamespace(execute=AsyncMock(side_effect=ConversationToolDenied("SECRET")))
    plan = SimpleNamespace(tool_name="query_hot_news", arguments={"question": "SECRET"})
    query = await service._execute_query(plan, "tenant", "user", "trace", False, {}, None,
                                        original_question="SECRET")
    hook = await service._start_confirmed_hook(name="start_writing_intent", arguments={"body": "SECRET"},
        tenant_id="tenant", user_id="user", permissions=frozenset(), conversation_id="conversation",
        request_id="request", trace_id="trace", original_content="SECRET", partial={}, on_bound=None)
    assert query.status == hook.status == "denied"
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["conversation.query"].attributes["newsagent.tool.name"] == "query_hot_news"
    assert spans["conversation.confirmed_hook"].attributes["newsagent.tool.name"] == "start_writing_intent"
    assert all(span.status.status_code is StatusCode.ERROR for span in spans.values())
    assert "SECRET" not in "".join(span.to_json() for span in spans.values())


@pytest.mark.asyncio
async def test_outbox_context_survives_database_envelope_and_redis_projection(capture):
    from app.schemas.events import ProgressEvent
    from app.services.outbox import OutboxService, OutboxRelay
    from tests.test_outbox import outbox_event, relay_settings
    exporter, _, tracer = capture
    row = outbox_event()
    session = Mock(execute=AsyncMock())
    with tracer.start_as_current_span("writing.checkpoint") as parent:
        await OutboxService.enqueue(session, ProgressEvent.model_validate(row.payload))
    row.payload = session.execute.await_args.args[0].compile().params["payload"]
    publisher = AsyncMock()
    await OutboxRelay(Mock(), publisher, relay_settings())._publish_one(row)
    published = publisher.publish.await_args.args[0].model_dump()
    assert "_telemetry" not in published
    span = next(s for s in exporter.get_finished_spans() if s.name == "outbox.publish")
    assert span.context.trace_id == parent.get_span_context().trace_id
    assert span.parent.span_id == parent.get_span_context().span_id
