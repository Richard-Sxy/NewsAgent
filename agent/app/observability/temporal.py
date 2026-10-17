"""Official replay-aware propagation with a privacy-preserving tracer facade.

The SDK's default exception events contain message/stacktrace. The facade strips
them before they reach processors, rather than relying on backend redaction.
"""
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from temporalio.contrib.opentelemetry import TracingInterceptor, TracingWorkflowInboundInterceptor
from temporalio import activity
from temporalio.worker import ActivityInboundInterceptor

from app.observability.settings import ObservabilitySettings
from app.observability.telemetry import get_tracer, operation, record_error, sanitize_attributes


class _SafeSpan(Span):
    def __init__(self, span):
        self._span = span

    def end(self, end_time=None):
        return self._span.end(end_time=end_time)

    def get_span_context(self):
        return self._span.get_span_context()

    def is_recording(self):
        return self._span.is_recording()

    def set_attribute(self, key, value):
        self.set_attributes({key: value})

    def set_attributes(self, attributes):
        # Official Temporal identity attributes are identifiers, never payloads.
        identifiers = {k: str(v)[:256] for k, v in attributes.items()
                       if k in {"temporalWorkflowID", "temporalRunID", "temporalActivityID", "temporalActivityType"}}
        self._span.set_attributes({**sanitize_attributes(attributes), **identifiers})

    def add_event(self, name, attributes=None, timestamp=None):
        if name == "exception":
            return
        self._span.add_event("temporal.event", sanitize_attributes(attributes or {}), timestamp)

    def update_name(self, name):
        self._span.update_name(name)

    def set_status(self, status, description=None):
        code = status.status_code if isinstance(status, Status) else status
        self._span.set_status(Status(code))

    def record_exception(self, exception, attributes=None, timestamp=None, escaped=False):
        record_error(self._span, exception, "temporal")


class SafeTemporalTracer(Tracer):
    def start_span(self, name, context=None, kind=trace.SpanKind.INTERNAL,
                   attributes=None, links=(), start_time=None,
                   record_exception=True, set_status_on_exception=True):
        span = get_tracer().start_span(name, context=context, kind=kind,
            links=links or (), start_time=start_time)
        wrapped = _SafeSpan(span)
        wrapped.set_attributes(attributes or {})
        return wrapped

    @contextmanager
    def start_as_current_span(self, name, context=None, kind=trace.SpanKind.INTERNAL,
                              attributes=None, links=(), start_time=None,
                              record_exception=True, set_status_on_exception=True, end_on_exit=True):
        span = self.start_span(name, context, kind, attributes, links, start_time)
        try:
            with trace.use_span(span, end_on_exit=False, record_exception=False,
                                set_status_on_exception=False):
                try:
                    yield span
                except BaseException as exc:
                    record_error(span, exc, "temporal")
                    raise
        finally:
            if end_on_exit:
                span.end()


class _WorkflowTracing(TracingWorkflowInboundInterceptor):
    def __init__(self, next):
        super().__init__(next)
        self.text_map_propagator = TraceContextTextMapPropagator()


class SafeTracingInterceptor(TracingInterceptor):
    def __init__(self):
        super().__init__(tracer=SafeTemporalTracer(), always_create_workflow_spans=True)
        self.text_map_propagator = TraceContextTextMapPropagator()

    def workflow_interceptor_class(self, input):
        super().workflow_interceptor_class(input)
        return _WorkflowTracing

    def intercept_activity(self, next):
        # Restore the remote trace before creating the per-attempt business span.
        return super().intercept_activity(_ActivityMetrics(next))


class _ActivityMetrics(ActivityInboundInterceptor):
    async def execute_activity(self, input):
        info = activity.info()
        attributes = {"temporal.activity.type": info.activity_type,
                      "temporal.activity.attempt": info.attempt,
                      "temporal.workflow.id": info.workflow_id or "",
                      "temporal.run.id": info.workflow_run_id or ""}
        # Durable IDs refer back to the authoritative run/job, never payloads.
        if input.args:
            request = input.args[0]
            for key in ("run_id", "job_id", "production_bundle_version"):
                value = getattr(request, key, None)
                if value is not None:
                    attributes["newsagent." + key] = str(value)
        with operation("temporal.activity", attributes=attributes):
            return await super().execute_activity(input)


def temporal_interceptors():
    return [SafeTracingInterceptor()] if ObservabilitySettings().enabled else []
