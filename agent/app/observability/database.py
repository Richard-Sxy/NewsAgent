"""SQL timing without SQL statements, bind values, DSNs or exception messages."""
from opentelemetry.trace import SpanKind
from sqlalchemy import event

from app.observability.telemetry import get_tracer, record_error


def instrument_engine(engine):
    def before(connection, cursor, statement, parameters, context, executemany):
        first = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "OTHER"
        verb = first if first in {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "BEGIN", "COMMIT", "ROLLBACK"} else "OTHER"
        span = get_tracer().start_span("db.query", kind=SpanKind.CLIENT,
            attributes={"db.system.name": "postgresql", "db.operation.name": verb})
        context._newsagent_span = span

    def after(connection, cursor, statement, parameters, context, executemany):
        span = getattr(context, "_newsagent_span", None)
        if span is not None:
            span.end()
            context._newsagent_span = None

    def failed(context):
        execution = context.execution_context
        span = getattr(execution, "_newsagent_span", None)
        if span is not None:
            record_error(span, context.original_exception, "database")
            span.end()
            execution._newsagent_span = None

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    event.listen(engine, "handle_error", failed)
