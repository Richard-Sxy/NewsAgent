"""Safe OpenTelemetry primitives shared by API, workers and model ports.

Only bounded technical attributes leave this module.  No exception message,
stack trace, prompt, document, model output, URL or HTTP headers are exported.
Providers belong to the runtime rather than irreversible global SDK singletons,
so application lifespan restarts and isolated tests remain usable.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
import math
import re
import sys
from threading import RLock
from time import perf_counter, time_ns
from typing import Any, Iterator, Mapping

from opentelemetry import metrics, trace
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from app.observability.settings import ObservabilitySettings


_SAFE_KEYS = frozenset({
    "newsagent.operation", "newsagent.stage", "newsagent.outcome", "newsagent.run_id",
    "newsagent.job_id", "newsagent.conversation_id", "newsagent.turn_id",
    "newsagent.tool.name", "newsagent.attempt", "newsagent.production_bundle_version",
    "newsagent.document_count", "newsagent.result_count", "newsagent.retryable", "newsagent.duration_ms",
    "http.request.method", "http.route", "http.response.status_code",
    "db.system.name", "db.operation.name", "temporal.workflow.id", "temporal.run.id",
    "temporal.workflow.run.id", "temporal.workflow.type", "temporal.activity.type",
    "temporal.activity.attempt", "error.type", "error.kind", "error.stage",
    "model.scene", "model.route", "model.prompt_version", "model.profile_id",
    "model.schema_name", "model.schema_version", "model.schema_hash", "model.attempt",
    "model.provider_request_id", "model.failure_kind", "model.retryable",
    "model.input_count", "model.embedding_dimensions", "model.request_timeout_seconds", "gen_ai.operation.name",
    "gen_ai.provider.name", "gen_ai.request.model", "gen_ai.response.model",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens",
    "gen_ai.token.type",
})
_METRIC_KEYS = frozenset({
    "newsagent.operation", "newsagent.stage", "newsagent.outcome", "newsagent.tool.name", "newsagent.retryable",
    "http.request.method", "http.route", "http.response.status_code", "db.system.name",
    "db.operation.name", "temporal.workflow.type", "temporal.activity.type",
    "error.type", "error.kind", "error.stage", "model.scene", "model.route",
    "model.failure_kind", "model.retryable", "gen_ai.operation.name", "gen_ai.provider.name",
    "gen_ai.token.type",
})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_./:{}@+\-]{1,128}$")
_EVENT = re.compile(r"^[a-z][a-z0-9_.]{0,95}$")
_lock = RLock()
_runtime: TelemetryRuntime | None = None
_noop_trace = trace.NoOpTracerProvider()
_noop_meter = metrics.NoOpMeterProvider()
_active_operations: ContextVar[tuple[dict[str, Any], ...]] = ContextVar("newsagent_operations", default=())


def sanitize_attributes(attributes: Mapping[str, Any] | None, *, metric: bool = False) -> dict[str, Any]:
    """列入许可名单的标量技术数据； ID 绝不是公制尺寸。"""
    allowed = _METRIC_KEYS if metric else _SAFE_KEYS
    result: dict[str, Any] = {}
    if not isinstance(attributes, Mapping):
        return result
    for key, value in attributes.items():
        if key not in allowed:
            continue
        if isinstance(value, bool) or isinstance(value, int):
            result[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            result[key] = value
        elif isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            result[key] = value
    return result


def set_safe_attributes(span: Span, attributes: Mapping[str, Any] | None) -> None:
    safe = sanitize_attributes(attributes)
    for state in reversed(_active_operations.get()):
        if state["span"] is span:
            state["attributes"].update(safe)
            break
    span.set_attributes(safe)


def _failure(error: BaseException, stage: str) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "error.type": type(error).__name__,
        "error.stage": getattr(error, "failure_stage", None) or stage,
        "newsagent.stage": getattr(error, "failure_stage", None) or stage,
        "newsagent.outcome": "error",
    }
    kind = getattr(error, "failure_kind", None)
    if kind:
        attributes["error.kind"] = kind
        attributes["model.failure_kind"] = kind
    retryable = getattr(error, "retryable", None)
    if isinstance(retryable, bool):
        attributes["model.retryable"] = retryable
        attributes["newsagent.retryable"] = retryable
    return sanitize_attributes(attributes)


def record_error(span: Span, error: BaseException, stage: str) -> None:
    """Record error class and static failure code, never str(error)."""
    failure = _failure(error, stage)
    for state in reversed(_active_operations.get()):
        if state["span"] is span:
            state["failure"] = failure
            break
    set_safe_attributes(span, failure)
    span.set_status(Status(StatusCode.ERROR))


def get_tracer(name: str = "newsagent") -> trace.Tracer:
    runtime = _runtime
    provider = runtime.tracer_provider if runtime and not runtime.closed else _noop_trace
    return provider.get_tracer(name)


def get_meter(name: str = "newsagent") -> metrics.Meter:
    runtime = _runtime
    provider = runtime.meter_provider if runtime and not runtime.closed else _noop_meter
    return provider.get_meter(name)


def record_metric(name: str, value: int | float, *, attributes: Mapping[str, Any] | None = None,
                  unit: str = "", kind: str = "histogram") -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        return
    runtime = _runtime
    if runtime is None or runtime.closed or not runtime.settings.enabled:
        return
    key = (name, unit, kind)
    with _lock:
        instrument = runtime.instruments.get(key)
        if instrument is None:
            meter = get_meter()
            instrument = (meter.create_counter(name, unit=unit) if kind == "counter"
                          else meter.create_histogram(name, unit=unit))
            runtime.instruments[key] = instrument
    if kind == "counter":
        instrument.add(value, sanitize_attributes(attributes, metric=True))
    else:
        instrument.record(value, sanitize_attributes(attributes, metric=True))


@contextmanager
def operation(name: str, *, attributes: Mapping[str, Any] | None = None,
              kind: SpanKind = SpanKind.INTERNAL, context: Any = None) -> Iterator[Span]:
    """这段代码产生追踪记录、指标和日志三类书籍，最终在哪里查看，还要自己看。"""
    attrs = sanitize_attributes(attributes)
    attrs.setdefault("newsagent.operation", name)
    attrs.setdefault("newsagent.stage", name)
    started = perf_counter()
    # 这边创建 Span + 保存上下文
    with get_tracer().start_as_current_span(
        name, attributes=attrs, kind=kind, context=context,
        record_exception=False, set_status_on_exception=False,
    ) as span:
        state = {"span": span, "failure": {}, "attributes": dict(attrs)}
        token = _active_operations.set((*_active_operations.get(), state))
        outcome = "success"
        try:
            yield span  # 这边是关键位置，执行到这边之后执行外部的 with 代码
        except BaseException as error:
            outcome = "error"
            record_error(span, error, attrs.get("newsagent.stage", name))
            attrs.update(_failure(error, attrs.get("newsagent.stage", name)))
            raise
        finally:
            try:
                attrs.update(sanitize_attributes(getattr(span, "attributes", None)))
                attrs.update(state["attributes"])
                attrs.update(state["failure"])
                if state["failure"] or getattr(getattr(span, "status", None), "status_code", None) == StatusCode.ERROR:
                    outcome = "error"
                attrs["newsagent.outcome"] = outcome
                elapsed = perf_counter() - started
                set_safe_attributes(span, {"newsagent.outcome": outcome})
                record_metric("newsagent.operation.calls", 1, attributes=attrs, unit="{call}", kind="counter")
                record_metric("newsagent.operation.duration", elapsed, attributes=attrs, unit="s")
                log_event("operation.failed" if outcome == "error" else "operation.completed",
                    attributes={**attrs, "newsagent.duration_ms": round(elapsed * 1000, 3)},
                    level=logging.ERROR if outcome == "error" else logging.INFO)
            finally:
                _active_operations.reset(token)


def _safe_log(record: logging.LogRecord) -> dict[str, Any]:
    event = getattr(record, "observability_event", "application.log")
    if not isinstance(event, str) or not _EVENT.fullmatch(event):
        event = "application.log"
    attributes = sanitize_attributes(getattr(record, "observability_attributes", None))
    if record.exc_info and record.exc_info[0]:
        attributes["error.type"] = record.exc_info[0].__name__
    current = trace.get_current_span().get_span_context()
    result = {
        "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
        "severity": record.levelname, "logger": record.name if _IDENTIFIER.fullmatch(record.name) else "application",
        "event": event, "trace_id": format(current.trace_id, "032x") if current.is_valid else None,
        "span_id": format(current.span_id, "016x") if current.is_valid else None,
        "attributes": attributes,
    }
    runtime = _runtime
    if runtime is not None:
        result["service.name"] = runtime.service_name
        result["service.version"] = runtime.settings.service_version
        result["deployment.environment.name"] = runtime.settings.environment
    return result


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_safe_log(record), ensure_ascii=False, separators=(",", ":"))


class _SafeOTLPLogHandler(logging.Handler):
    def __init__(self, provider: LoggerProvider) -> None:
        super().__init__()
        self.otel_logger = provider.get_logger("newsagent")

    def emit(self, record: logging.LogRecord) -> None:
        # Exporter diagnostics must not be recursively exported by their own handler.
        if record.name.startswith("opentelemetry"):
            return
        safe = _safe_log(record)
        current = trace.get_current_span().get_span_context()
        severity = {
            logging.DEBUG: SeverityNumber.DEBUG, logging.INFO: SeverityNumber.INFO,
            logging.WARNING: SeverityNumber.WARN, logging.ERROR: SeverityNumber.ERROR,
            logging.CRITICAL: SeverityNumber.FATAL,
        }.get(record.levelno, SeverityNumber.INFO)
        self.otel_logger.emit(LogRecord(
            timestamp=int(record.created * 1_000_000_000), observed_timestamp=time_ns(),
            trace_id=current.trace_id, span_id=current.span_id, trace_flags=current.trace_flags,
            severity_number=severity, severity_text=record.levelname, body=safe["event"],
            attributes=safe["attributes"],
        ))


def log_event(event: str, *, attributes: Mapping[str, Any] | None = None, level: int = logging.INFO) -> None:
    logging.getLogger("newsagent").log(level, "", extra={
        "observability_event": event, "observability_attributes": sanitize_attributes(attributes),
    })


@dataclass
class TelemetryRuntime:
    settings: ObservabilitySettings
    tracer_provider: Any
    meter_provider: Any
    logger_provider: LoggerProvider | None = None
    service_name: str = "newsagent-api"
    handlers: list[logging.Handler] = field(default_factory=list)
    previous_handlers: list[logging.Handler] = field(default_factory=list)
    previous_level: int = logging.WARNING
    previous_named_loggers: list[tuple[logging.Logger, list[logging.Handler], bool, int]] = field(default_factory=list)
    instruments: dict[tuple[str, str, str], Any] = field(default_factory=dict)
    closed: bool = False

    def force_flush(self) -> None:
        timeout = int(self.settings.export_timeout_seconds * 1000)
        for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
            if provider is not None and hasattr(provider, "force_flush"):
                try:
                    provider.force_flush(timeout_millis=timeout)
                except Exception as error:
                    log_event("telemetry.flush.failed", attributes=_failure(error, "telemetry.flush"), level=logging.ERROR)

    def shutdown(self) -> None:
        global _runtime
        with _lock:
            if self.closed:
                return
            try:
                self.force_flush()
            finally:
                for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
                    if provider is not None and hasattr(provider, "shutdown"):
                        try:
                            provider.shutdown()
                        except Exception as error:
                            log_event("telemetry.shutdown.failed", attributes=_failure(error, "telemetry.shutdown"), level=logging.ERROR)
                root = logging.getLogger()
                for handler in self.handlers:
                    root.removeHandler(handler)
                    handler.close()
                if self.settings.json_logs:
                    for handler in self.previous_handlers:
                        root.addHandler(handler)
                if self.settings.json_logs or self.settings.enabled:
                    root.setLevel(self.previous_level)
                for logger, handlers, propagate, level in self.previous_named_loggers:
                    logger.handlers = handlers
                    logger.propagate = propagate
                    logger.setLevel(level)
                self.closed = True
                if _runtime is self:
                    _runtime = None


def configure_telemetry(service_name: str | None = None, *, settings: ObservabilitySettings | None = None,
                        span_exporter: Any = None, metric_reader: Any = None,
                        log_exporter: Any = None) -> TelemetryRuntime:
    """Configure SDK providers once per active process lifespan, OTLP HTTP only.

    In-memory exporters/readers may be injected for deterministic contract tests.
    Disabled mode constructs no exporter and makes no network requests.
    """
    global _runtime
    with _lock:
        if _runtime is not None and not _runtime.closed:
            return _runtime
        config = settings or ObservabilitySettings()
        tracer_provider: Any = _noop_trace
        meter_provider: Any = _noop_meter
        logger_provider = None
        if config.enabled:
            resource = Resource({"service.name": service_name or config.service_name,
                                 "service.version": config.service_version,
                                 "deployment.environment.name": config.environment})
            if span_exporter is None or metric_reader is None or log_exporter is None:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
                from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

            timeout = config.export_timeout_seconds
            tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False,
                sampler=ParentBased(TraceIdRatioBased(config.sample_ratio)))
            if span_exporter is not None:
                tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
            else:
                tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                    endpoint=config.otlp_endpoint + "/v1/traces", timeout=timeout)))
            if metric_reader is None:
                metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(
                    endpoint=config.otlp_endpoint + "/v1/metrics", timeout=timeout),
                    export_interval_millis=config.metric_export_interval_ms,
                    export_timeout_millis=timeout * 1000)
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader], shutdown_on_exit=False)
            logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
            if log_exporter is not None:
                logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
            else:
                logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(
                    endpoint=config.otlp_endpoint + "/v1/logs", timeout=timeout)))
        runtime = TelemetryRuntime(config, tracer_provider, meter_provider, logger_provider)
        runtime.service_name = service_name or config.service_name
        root = logging.getLogger()
        runtime.previous_level = root.level
        if config.json_logs:
            runtime.previous_handlers = list(root.handlers)
            for handler in runtime.previous_handlers:
                root.removeHandler(handler)
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(SafeJsonFormatter())
            root.addHandler(stream)
            runtime.handlers.append(stream)
            root.setLevel(config.log_level)
            # Uvicorn owns handlers outside root by default; route them through
            # the same safety boundary so raw URLs and tracebacks cannot escape.
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
                logger = logging.getLogger(name)
                runtime.previous_named_loggers.append((logger, list(logger.handlers), logger.propagate, logger.level))
                logger.handlers = []
                logger.propagate = True
                logger.setLevel(logging.NOTSET)
        if logger_provider is not None:
            handler = _SafeOTLPLogHandler(logger_provider)
            handler.setLevel(config.log_level)
            root.addHandler(handler)
            runtime.handlers.append(handler)
            root.setLevel(config.log_level)
        _runtime = runtime
        return runtime
