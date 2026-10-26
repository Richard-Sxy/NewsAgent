"""Real SDK contracts: correlation, privacy, failure attribution and lifecycle."""

import io
import json
import logging
from types import SimpleNamespace

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
import pytest

from app.observability.settings import ObservabilitySettings
from app.observability.telemetry import (
    SafeJsonFormatter, configure_telemetry, log_event, operation, record_error,
    record_metric, sanitize_attributes, set_safe_attributes,
)


@pytest.fixture
def telemetry():
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    reader = InMemoryMetricReader()
    runtime = configure_telemetry(settings=ObservabilitySettings(enabled=True, json_logs=False),
        span_exporter=spans, metric_reader=reader, log_exporter=logs)
    yield SimpleNamespace(runtime=runtime, spans=spans, logs=logs, reader=reader)
    runtime.shutdown()


def metric_points(reader, name):
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [point for resource in data.resource_metrics for scope in resource.scope_metrics
            for metric in scope.metrics if metric.name == name for point in metric.data.data_points]


def test_disabled_runtime_never_creates_an_exporter_or_reads_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OBSERVABILITY_ENABLED=true\nOBSERVABILITY_ENVIRONMENT=secret\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OBSERVABILITY_ENABLED", raising=False)
    monkeypatch.delenv("OBSERVABILITY_ENVIRONMENT", raising=False)
    settings = ObservabilitySettings(json_logs=False)
    assert settings.enabled is False and settings.environment == "local"
    runtime = configure_telemetry(settings=settings)
    try:
        with operation("disabled") as span:
            assert not span.is_recording()
        assert runtime.logger_provider is None
    finally:
        runtime.shutdown()


def test_real_sdk_nested_spans_log_correlation_and_no_payload(telemetry):
    with operation("conversation.turn", attributes={"newsagent.turn_id": "turn-1", "prompt": "secret"}) as parent:
        with operation("model.inference", attributes={"model.scene": "conversation"}) as child:
            set_safe_attributes(child, {"model.prompt_version": "v2", "gen_ai.usage.input_tokens": 12,
                "model.output": "secret", "http.request.header.authorization": "Bearer secret"})
            log_event("model.completed", attributes={"newsagent.turn_id": "turn-1", "response": "secret"})
    parent_span, child_span = sorted(telemetry.spans.get_finished_spans(), key=lambda s: s.name)
    assert child_span.name == "model.inference" and parent_span.name == "conversation.turn"
    assert child_span.parent.span_id == parent.get_span_context().span_id
    assert child_span.context.trace_id == parent_span.context.trace_id
    assert child_span.attributes["model.prompt_version"] == "v2"
    assert child_span.attributes["gen_ai.usage.input_tokens"] == 12
    exported = telemetry.logs.get_finished_logs()[0].log_record
    assert exported.trace_id == child_span.context.trace_id and exported.span_id == child_span.context.span_id
    assert exported.body == "model.completed"
    assert "secret" not in repr(telemetry.spans.get_finished_spans()) + repr(exported.attributes)
    assert "prompt" not in parent_span.attributes and "model.output" not in child_span.attributes


def test_instance_identity_separates_process_metrics(telemetry):
    record_metric("diagnostic.calls",1,kind="counter")
    data = telemetry.reader.get_metrics_data()
    instance = data.resource_metrics[0].resource.attributes["service.instance.id"]
    assert instance == telemetry.runtime.settings.service_instance_id
    assert ObservabilitySettings().service_instance_id != instance


def test_exception_attribution_preserves_failure_stage_without_messages(telemetry):
    class ModelFailure(RuntimeError):
        failure_stage = "response_decode"
        failure_kind = "invalid_json"

    with pytest.raises(ModelFailure):
        with operation("model.structured"):
            raise ModelFailure("private prompt and bearer password")
    span = telemetry.spans.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is None and not span.events
    assert span.attributes["error.stage"] == "response_decode"
    assert span.attributes["error.kind"] == "invalid_json"
    assert "private" not in str(dict(span.attributes))
    point = metric_points(telemetry.reader, "newsagent.operation.calls")[0]
    assert point.value == 1 and point.attributes["newsagent.outcome"] == "error"
    assert point.attributes["newsagent.stage"] == "response_decode"


def test_handled_failures_cannot_be_counted_as_success(telemetry):
    with operation("outbox.dispatch") as span:
        record_error(span, RuntimeError("private"), "publish")
    with operation("conversation.failed_result") as span:
        span.set_status(Status(StatusCode.ERROR))
    assert all(span.attributes["newsagent.outcome"] == "error"
               for span in telemetry.spans.get_finished_spans())
    assert all(p.attributes["newsagent.outcome"] == "error"
               for p in metric_points(telemetry.reader, "newsagent.operation.calls"))


def test_handled_failure_metrics_and_metadata_do_not_depend_on_trace_sampling():
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    reader = InMemoryMetricReader()
    runtime = configure_telemetry(settings=ObservabilitySettings(enabled=True, json_logs=False, sample_ratio=0),
        span_exporter=spans, metric_reader=reader, log_exporter=logs)
    try:
        with operation("sampled.out") as span:
            set_safe_attributes(span, {"model.prompt_version": "v3"})
            error = RuntimeError("private")
            error.retryable = True
            record_error(span, error, "publish")
        assert not spans.get_finished_spans()
        point = metric_points(reader, "newsagent.operation.calls")[0]
        assert point.attributes["newsagent.outcome"] == "error"
        assert point.attributes["error.stage"] == "publish"
        assert point.attributes["newsagent.retryable"] is True
        exported = logs.get_finished_logs()[0].log_record
        assert exported.body == "operation.failed"
        assert exported.attributes["model.prompt_version"] == "v3"
        assert exported.attributes["model.retryable"] is True
    finally:
        runtime.shutdown()


def test_metric_dimensions_exclude_ids_versions_and_payloads(telemetry):
    attrs = {"newsagent.turn_id": "turn-1", "model.prompt_version": "v3", "model.scene": "chat",
             "model.route": "local", "prompt": "private", "newsagent.outcome": "success"}
    record_metric("newsagent.model.calls", 1, attributes=attrs, kind="counter", unit="{call}")
    record_metric("newsagent.model.tokens", 0, attributes={**attrs, "gen_ai.token.type": "input"},
                  kind="counter", unit="{token}")
    record_metric("newsagent.model.tokens", -1, attributes=attrs, kind="counter", unit="{token}")
    point = metric_points(telemetry.reader, "newsagent.model.calls")[0]
    assert dict(point.attributes) == {"model.scene": "chat", "model.route": "local", "newsagent.outcome": "success"}
    assert metric_points(telemetry.reader, "newsagent.model.tokens")[0].value == 0


def test_freeform_logs_exception_and_extras_are_dropped_before_otlp(telemetry):
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(SafeJsonFormatter())
    logger = logging.getLogger("privacy.test")
    logger.addHandler(handler)
    try:
        try:
            raise ValueError("response body password")
        except ValueError:
            logger.exception("Bearer secret prompt=%s", "private", extra={"authorization": "secret"})
    finally:
        logger.removeHandler(handler)
    safe = json.loads(buffer.getvalue())
    assert safe["event"] == "application.log" and safe["attributes"] == {"error.type": "ValueError"}
    assert all(word not in buffer.getvalue() for word in ["password", "secret", "private", "Bearer", "Traceback"])
    exported = telemetry.logs.get_finished_logs()[0].log_record
    assert exported.body == "application.log" and exported.attributes == {"error.type": "ValueError"}


def test_shutdown_is_idempotent_and_a_new_lifespan_can_record_again():
    for _ in range(2):
        spans = InMemorySpanExporter()
        runtime = configure_telemetry(settings=ObservabilitySettings(enabled=True, json_logs=False),
            span_exporter=spans, metric_reader=InMemoryMetricReader(), log_exporter=InMemoryLogRecordExporter())
        with operation("lifespan.operation"):
            pass
        runtime.force_flush()
        assert len(spans.get_finished_spans()) == 1
        runtime.shutdown()
        runtime.shutdown()


def test_attribute_whitelist_rejects_injection_and_non_scalars():
    assert sanitize_attributes({"model.scene": "secret\nbody", "error.type": "Bad Error",
        "newsagent.turn_id": ["x"], "gen_ai.usage.input_tokens": float("nan"), "headers": "token"}) == {}
    assert sanitize_attributes("not a mapping") == {}
    assert sanitize_attributes({"newsagent.run_id": "abc", "model.scene": "chat"}, metric=True) == {"model.scene": "chat"}


def test_uvicorn_private_handlers_are_sanitized_and_restored():
    logger = logging.getLogger("uvicorn.access")
    previous_handlers = list(logger.handlers)
    previous_propagate = logger.propagate
    previous_level = logger.level
    private = logging.StreamHandler(io.StringIO())
    logger.handlers = [private]
    logger.propagate = False
    runtime = configure_telemetry(settings=ObservabilitySettings(enabled=False, json_logs=True))
    output = io.StringIO()
    runtime.handlers[0].setStream(output)
    try:
        logger.info("GET /?token=private-prompt")
        assert logger.handlers == [] and logger.propagate is True
        safe = json.loads(output.getvalue())
        assert safe["event"] == "application.log"
        assert safe["service.name"] == "newsagent-api"
        assert "private-prompt" not in output.getvalue()
    finally:
        runtime.shutdown()
        assert logger.handlers == [private] and logger.propagate is False
        logger.handlers = previous_handlers
        logger.propagate = previous_propagate
        logger.setLevel(previous_level)


@pytest.mark.parametrize("endpoint", ["ftp://localhost", "http://user:password@localhost", "https://a/?token=private"])
def test_otlp_configuration_rejects_secret_bearing_urls(endpoint):
    with pytest.raises(ValueError):
        ObservabilitySettings(otlp_endpoint=endpoint)


def test_real_otlp_http_exports_all_signals_with_correlated_safe_payloads():
    """A real HTTP sink decodes the protobuf sent by the production exporters."""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    requests = []

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *_):
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    except PermissionError:
        pytest.skip("Execution sandbox does not permit a local HTTP listener")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = None
    try:
        settings = ObservabilitySettings(enabled=True, json_logs=False,
            otlp_endpoint=f"http://127.0.0.1:{server.server_port}")
        runtime = configure_telemetry(settings=settings)
        with operation("otlp.contract", attributes={"newsagent.turn_id": "turn-http", "prompt": "private-body"}):
            log_event("otlp.completed", attributes={"newsagent.turn_id": "turn-http", "output": "private-body"})
        runtime.force_flush()
        payloads = {path: body for path, body in requests}
        assert {"/v1/traces", "/v1/metrics", "/v1/logs"} <= payloads.keys()
        span_request = ExportTraceServiceRequest.FromString(payloads["/v1/traces"])
        metric_request = ExportMetricsServiceRequest.FromString(payloads["/v1/metrics"])
        log_request = ExportLogsServiceRequest.FromString(payloads["/v1/logs"])
        exported_span = span_request.resource_spans[0].scope_spans[0].spans[0]
        exported_log = log_request.resource_logs[0].scope_logs[0].log_records[0]
        assert exported_span.trace_id == exported_log.trace_id
        assert exported_span.span_id == exported_log.span_id
        metric_names = {metric.name for resource in metric_request.resource_metrics
                        for scope in resource.scope_metrics for metric in scope.metrics}
        assert {"newsagent.operation.calls", "newsagent.operation.duration"} <= metric_names
        assert all(b"private-body" not in body for _, body in requests)
    finally:
        if runtime is not None:
            runtime.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
