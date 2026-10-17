"""Own SDK lifecycle once per API/worker process, including startup failures."""
import asyncio
from functools import wraps

from app.observability.telemetry import configure_telemetry


def telemetry_process(service_name):
    def decorate(function):
        @wraps(function)
        async def call(*args, **kwargs):
            runtime = configure_telemetry(service_name)
            try:
                return await function(*args, **kwargs)
            finally:
                # Exporter shutdown may block; keep event loop available.
                await asyncio.to_thread(runtime.shutdown)
        return call
    return decorate
