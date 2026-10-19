from functools import wraps
from inspect import signature

from app.observability.telemetry import operation, record_error, set_safe_attributes

class DamainOutcomeError(RuntimeError):
    """这边定义一个自定义报错。"""

def trace(name, *, attributes=None, identity=None, result_status=False):

    def decorate(function):
        parameters = signature(function)

        @wraps
        async def call(*args, **kwargs):
            selected = dict(attributes or {})
            if identity:
                bound = parameters.bind(*args, **kwargs).arguments
            for source, target in identity.items():
                key, *fields = source.split()
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
                    if status in {"completed", "interrupted", "denied", "processing"}:
                        set_safe_attributes(span, {"newsagent.outcome": status})
                    if status in {"failed", "interrupted", "denied"}:
                        record_error(span, DamainOutcomeError(), name)
                return result
        return call
    return decorate