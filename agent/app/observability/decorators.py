"""Business boundaries, including failures returned as ordinary domain results."""
from functools import wraps
from inspect import signature

from app.observability.telemetry import operation, record_error, set_safe_attributes


class DomainOutcomeError(RuntimeError):
    """A bounded domain operation returned a failed result without raising."""


def traced(name, *, attributes=None, identity=None, result_status=False):
    """Only explicitly selected identifiers enter spans; never serialize arguments."""
    def decorate(function):
        parameters = signature(function)

        @wraps(function)
        async def call(*args, **kwargs):
            selected = dict(attributes or {})
            if identity:
                bound = parameters.bind(*args, **kwargs).arguments
                for source, target in identity.items():
                    key, *fields = source.split(".")
                    value = bound.get(key)
                    for field in fields:
                        value = getattr(value, field, None)
                    if value is not None:
                        selected[target] = str(value)
            with operation(name, attributes=selected) as span:
                result = await function(*args, **kwargs)
                if result_status:
                    status = getattr(result, "status", None)
                    status = getattr(status, "value", status)
                    # The vocabulary comes from application enums, never model text.
                    if status in {"completed", "failed", "interrupted", "processing", "denied"}:
                        set_safe_attributes(span, {"newsagent.outcome": status})
                    if status in {"failed", "interrupted", "denied"}:
                        record_error(span, DomainOutcomeError(), name)
                return result
        return call
    return decorate
