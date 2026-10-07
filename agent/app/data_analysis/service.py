"""独立分析服务端。提供私有分析接口，验证请求、限制资源、并调用分析执行起。他与 NewsAgent 主 API 分开运行。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import sys

from fastapi import FastAPI, Request
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from .engine import AnalysisInputError, validate_request
from .runner import (
    AnalysisExecutionError, AnalysisRunner, _json_bytes, _reject_constant,
    _unique_object,
)


class AnalysisServiceSettings(BaseSettings):
    """Isolated environment configuration; deliberately never reads .env."""

    model_config = SettingsConfigDict(
        env_prefix="ANALYSIS_SERVICE_", env_file=None, extra="forbid",
        hide_input_in_errors=True, frozen=True,
    )
    token: SecretStr
    environment: str = "development"
    require_linux_limits: bool = True
    timeout_seconds: float = Field(default=3, gt=0, le=60, allow_inf_nan=False)
    request_timeout_seconds: float = Field(default=5, gt=0, le=60, allow_inf_nan=False)
    max_rows: int = Field(default=200, ge=1, le=1000)
    max_input_bytes: int = Field(default=262_144, ge=1, le=1_048_576)
    max_output_bytes: int = Field(default=262_144, ge=1, le=1_048_576)
    max_concurrency: int = Field(default=2, ge=1, le=32)
    memory_mb: int = Field(default=128, ge=32, le=4096)
    tls_certfile: str | None = None
    tls_keyfile: str | None = None

    @model_validator(mode="after")
    def validate_runtime(self) -> "AnalysisServiceSettings":
        token = self.token.get_secret_value()
        if (not 16 <= len(token) <= 4096 or not token.isascii()
                or any(character.isspace() or ord(character) < 33
                       or ord(character) == 127 for character in token)):
            raise ValueError("Invalid analysis service authentication configuration")
        if self.environment not in {"development", "production"}:
            raise ValueError("Unsupported analysis service environment")
        if self.timeout_seconds > self.request_timeout_seconds:
            raise ValueError("Worker budget exceeds analysis request budget")
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            raise ValueError("Analysis TLS certificate and key must be configured together")
        for path in (self.tls_certfile, self.tls_keyfile):
            if path is not None and (
                not path.strip() or len(path) > 2048
                or any(ord(character) < 32 or ord(character) == 127 for character in path)
            ):
                raise ValueError("Invalid analysis TLS file configuration")
        if self.environment == "production" and not (
            self.tls_certfile and self.tls_keyfile
        ):
            raise ValueError("Production analysis service requires TLS certificate and key")
        if self.environment == "production" and (
            not self.require_linux_limits or not sys.platform.startswith("linux")
        ):
            raise ValueError("Production analysis service requires Linux resource limits")
        return self


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error_code": code, "retryable": False}, status_code=status)


async def _body(request: Request, maximum: int) -> bytes:
    lengths = request.headers.getlist("content-length")
    if lengths:
        if (len(lengths) != 1 or not lengths[0].isascii()
                or not lengths[0].isdigit() or len(lengths[0]) > 10):
            raise AnalysisExecutionError("invalid_request")
        if int(lengths[0]) > maximum:
            raise AnalysisExecutionError("input_limit")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise AnalysisExecutionError("input_limit")
        body.extend(chunk)
    return bytes(body)


async def _cancel_tasks(tasks) -> bool:
    """Finish cleanup despite repeated cancellation, then report interruption.

    Shielding the gather prevents shutdown and an HTTP cancellation racing to
    cancel a worker a second time while AnalysisRunner is collecting its handle
    or reaping it. Callers re-raise after every dependent task has been cleaned.
    """
    tasks = tuple(tasks)
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    completion = asyncio.gather(*tasks, return_exceptions=True)
    interrupted = False
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            interrupted = True
    completion.result()
    return interrupted


def create_app(
    settings: AnalysisServiceSettings | None = None,
    runner: AnalysisRunner | None = None,
) -> FastAPI:
    """Build only the authenticated private analysis API.

    A slot is reserved before body collection, with no wait queue. The total
    budget covers collection, JSON validation, execution and serialization.
    Cancellation reaches AnalysisRunner, which kills and reaps the worker.
    """
    settings = settings or AnalysisServiceSettings()
    validator = AnalysisRunner(
        backend="process", timeout_seconds=settings.timeout_seconds,
        max_rows=settings.max_rows, max_input_bytes=settings.max_input_bytes,
        max_output_bytes=settings.max_output_bytes,
        max_concurrency=settings.max_concurrency, memory_mb=settings.memory_mb,
    )
    executor = runner or validator
    active: set[asyncio.Task] = set()
    accepting = True
    occupied = 0
    expected_authorization = (
        "Bearer " + settings.token.get_secret_value()
    ).encode("ascii")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal accepting
        accepting = True
        try:
            yield
        finally:
            accepting = False
            interrupted = await _cancel_tasks(tuple(active))
            if interrupted:
                raise asyncio.CancelledError

    app = FastAPI(
        title="Private aggregate analysis", docs_url=None, redoc_url=None,
        openapi_url=None, lifespan=lifespan,
    )

    def authorized(request: Request) -> bool:
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            return False
        try:
            supplied = values[0].encode("ascii")
        except UnicodeError:
            return False
        return hmac.compare_digest(supplied, expected_authorization)

    @app.get("/health")
    async def health(request: Request):
        if not authorized(request):
            return _error("unauthorized", 401)
        if not accepting:
            return _error("service_unavailable", 503)
        return {"status": "ok"}

    async def collect_and_run(request: Request) -> JSONResponse:
        raw = await _body(request, settings.max_input_bytes)
        try:
            submitted = json.loads(
                raw.decode("utf-8"), parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
            normalized = validate_request(submitted)
            payload = _json_bytes(normalized)
        except (AnalysisInputError, ValueError, TypeError, UnicodeError,
                OverflowError, RecursionError):
            raise AnalysisExecutionError("invalid_request") from None
        total_rows = sum(
            len(normalized[key]["rows"])
            for key in ("dataset", "reference_dataset") if key in normalized
        )
        if total_rows > settings.max_rows:
            raise AnalysisExecutionError("row_limit")
        if len(payload) > settings.max_input_bytes:
            raise AnalysisExecutionError("input_limit")
        working = asyncio.create_task(executor.run(normalized))

        async def disconnected() -> None:
            while True:
                event = await request.receive()
                if event["type"] == "http.disconnect":
                    raise ClientDisconnect()

        monitoring = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait(
                {working, monitoring}, return_when=asyncio.FIRST_COMPLETED,
            )
            if monitoring in done:
                await monitoring
            report = await working
            try:
                # Defence in depth: no malformed worker/injected Port result is
                # exposed, even if an executor bypasses the normal runner path.
                if type(report) is not dict:
                    raise ValueError
                execution = report.get("execution")
                if (type(execution) is not dict or set(execution) != {
                        "backend", "elapsed_ms", "input_sha256", "limits"}
                        or execution["backend"] != "process"
                        or type(execution["elapsed_ms"]) is not int
                        or execution["elapsed_ms"] < 0
                        or execution["input_sha256"] != hashlib.sha256(payload).hexdigest()
                        or execution["limits"] != validator.description()["resource_limits"]):
                    raise ValueError
                core = {key: value for key, value in report.items() if key != "execution"}
                validator._validate_output(_json_bytes(core), normalized)
                encoded = _json_bytes(report)
            except (ValueError, TypeError, OverflowError, RecursionError):
                raise AnalysisExecutionError("invalid_output") from None
            if len(encoded) > settings.max_output_bytes:
                raise AnalysisExecutionError("output_limit")
            return JSONResponse(report)
        finally:
            interrupted = await _cancel_tasks((monitoring, working))
            if interrupted:
                raise asyncio.CancelledError

    @app.post("/v1/analyze")
    async def analyze_request(request: Request):
        nonlocal occupied
        deadline = asyncio.get_running_loop().time() + settings.request_timeout_seconds
        if not authorized(request):
            return _error("unauthorized", 401)
        if not accepting:
            return _error("service_unavailable", 503)
        if occupied >= settings.max_concurrency:
            return _error("service_busy", 429)
        occupied += 1
        task = asyncio.create_task(collect_and_run(request))
        active.add(task)
        try:
            async with asyncio.timeout_at(deadline):
                result = await task
                if asyncio.get_running_loop().time() >= deadline:
                    return _error("timeout", 504)
                return result
        except (asyncio.TimeoutError, TimeoutError):
            return _error("timeout", 504)
        except ClientDisconnect:
            return _error("interrupted", 499)
        except AnalysisExecutionError as error:
            statuses = {
                "invalid_request": 400, "row_limit": 413, "input_limit": 413,
                "timeout": 504, "invalid_output": 502, "output_limit": 502,
                "resource_limit": 502, "worker_failed": 502,
                "worker_unavailable": 503,
            }
            code = error.error_code if error.error_code in statuses else "worker_failed"
            return _error(code, statuses[code])
        except Exception:
            # No input, exception, traceback, paths or secrets enter the response.
            return _error("worker_failed", 502)
        finally:
            interrupted = await _cancel_tasks((task,))
            active.discard(task)
            occupied -= 1
            if interrupted:
                raise asyncio.CancelledError

    return app


def main() -> None:
    import uvicorn

    settings = AnalysisServiceSettings()
    uvicorn.run(
        create_app(settings), host="0.0.0.0", port=8100, access_log=False,
        server_header=False, proxy_headers=False, workers=1,
        ssl_certfile=settings.tls_certfile, ssl_keyfile=settings.tls_keyfile,
    )


if __name__ == "__main__":
    main()
