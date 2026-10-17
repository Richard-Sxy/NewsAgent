"""Pure ASGI instrumentation keeps streaming responses inside their server span."""
from time import monotonic

from opentelemetry.context import Context
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.observability.telemetry import get_meter, get_tracer, record_error, set_safe_attributes


class TelemetryMiddleware:
    def __init__(self, app):
        self.app = app
        self._meter = None
        self._requests = self._duration = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Skip probe/scrape loops. Query strings, raw URLs and headers never export.
        if scope.get("path") in {"/health", "/ready", "/metrics"}:
            return await self.app(scope, receive, send)
        carrier = {key.decode("latin1"): value.decode("latin1")
                   for key, value in scope.get("headers", [])
                   if key in {b"traceparent", b"tracestate"} and len(value) <= 512}
        parent = TraceContextTextMapPropagator().extract(carrier, context=Context())
        method = scope.get("method", "OTHER")
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            method = "OTHER"
        start = monotonic()
        status = 500
        with get_tracer().start_as_current_span(
            "HTTP request", context=parent, kind=SpanKind.SERVER,
            record_exception=False, set_status_on_exception=False,
        ) as span:
            async def traced_send(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    context = span.get_span_context()
                    headers = [(k, v) for k, v in message.get("headers", [])
                               if k.lower() not in {b"x-trace-id", b"traceparent"}]
                    if context.is_valid:
                        response_carrier = {}
                        TraceContextTextMapPropagator().inject(response_carrier)
                        headers.append((b"x-trace-id", format(context.trace_id, "032x").encode()))
                        headers.append((b"traceparent", response_carrier["traceparent"].encode()))
                    message = {**message, "headers": headers}
                await send(message)
            try:
                await self.app(scope, receive, traced_send)
            except BaseException as exc:
                record_error(span, exc, "http")
                raise
            finally:
                route = getattr(scope.get("route"), "path", "unmatched")
                attrs = {"http.request.method": method, "http.route": route,
                         "http.response.status_code": status}
                set_safe_attributes(span, attrs)
                span.update_name(f"{method} {route}")
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                meter = get_meter()
                if meter is not self._meter:
                    self._meter = meter
                    self._requests = meter.create_counter("newsagent.http.requests")
                    self._duration = meter.create_histogram("newsagent.http.duration", unit="s")
                self._requests.add(1, attrs)
                self._duration.record(monotonic() - start, attrs)
