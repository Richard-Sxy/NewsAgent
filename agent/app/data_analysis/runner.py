"""管理本地分析执行。AnalysisRunner 启动固定的子进程或 Docker 容器，限制并发、超时、数据量和输出大小，并校验返回结果。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
import time
from uuid import uuid4

from .engine import AnalysisInputError, analyze, validate_request


_ENV = {"LANG": "C.UTF-8"}
_MAX_IO_BYTES = 1_048_576
_WORKER = Path(__file__).resolve().with_name("worker.py")


class AnalysisExecutionError(RuntimeError):
    """Safe, non-retryable execution failure suitable for an API error envelope."""

    retryable = False

    def __init__(self, error_code: str):
        self.error_code = error_code
        super().__init__(f"Data analysis failed: {error_code}")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class AnalysisRunner:
    def __init__(
        self,
        backend: str = "process",
        timeout_seconds: float = 3,
        max_rows: int = 200,
        max_input_bytes: int = 262_144,
        max_output_bytes: int = 262_144,
        max_concurrency: int = 2,
        memory_mb: int = 128,
        docker_image: str = "news-agent/data-analysis:local",
    ) -> None:
        if backend not in {"process", "docker"}:
            raise ValueError("Unsupported analysis execution backend")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("Invalid analysis timeout")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("Invalid analysis timeout")
        integer_limits = (
            (max_rows, 1, 1000),
            (max_input_bytes, 1, _MAX_IO_BYTES),
            (max_output_bytes, 1, _MAX_IO_BYTES),
            (max_concurrency, 1, 32),
            (memory_mb, 32, 4096),
        )
        if any(type(value) is not int or not low <= value <= high
               for value, low, high in integer_limits):
            raise ValueError("Invalid analysis resource limits")
        if (not isinstance(docker_image, str) or not docker_image
                or len(docker_image) > 256 or docker_image.startswith("-")
                or any(char.isspace() for char in docker_image)):
            raise ValueError("Invalid analysis Docker image")
        self.backend = backend
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows
        self.max_input_bytes = max_input_bytes
        self.max_output_bytes = max_output_bytes
        self.max_concurrency = max_concurrency
        self.memory_mb = memory_mb
        self.docker_image = docker_image
        self._slots = asyncio.Semaphore(max_concurrency)

    def description(self) -> dict:
        return {
            "execution_backend": self.backend,
            "resource_limits": {
                "timeout_seconds": self.timeout_seconds,
                "max_rows": self.max_rows,
                "max_input_bytes": self.max_input_bytes,
                "max_output_bytes": self.max_output_bytes,
                "max_concurrency": self.max_concurrency,
                "memory_mb": self.memory_mb,
                "os_resource_limits_enforced": (
                    self.backend == "docker" or sys.platform.startswith("linux")
                ),
            },
        }

    async def run(self, request: dict) -> dict:
        try:
            # Bound both the original input and its normalized representation.
            if len(_json_bytes(request)) > self.max_input_bytes:
                raise AnalysisExecutionError("input_limit")
            if not isinstance(request, dict):
                raise AnalysisExecutionError("invalid_request")
            dataset = request.get("dataset")
            if not isinstance(dataset, dict) or not isinstance(dataset.get("rows"), list):
                raise AnalysisExecutionError("invalid_request")
            reference = request.get("reference_dataset")
            reference_count = len(reference.get("rows", [])) if isinstance(reference, dict) and isinstance(reference.get("rows", []), list) else 0
            if len(dataset["rows"]) + reference_count > self.max_rows:
                raise AnalysisExecutionError("row_limit")
            normalized = validate_request(request)
            payload = _json_bytes(normalized)
        except (AnalysisInputError, TypeError, ValueError, OverflowError, RecursionError):
            raise AnalysisExecutionError("invalid_request") from None
        if len(payload) > self.max_input_bytes:
            raise AnalysisExecutionError("input_limit")
        async with self._slots:
            started = time.monotonic()
            stdout, _stderr, returncode = await self._execute(payload)
            if returncode != 0:
                code = "docker_failed" if self.backend == "docker" else "worker_failed"
                try:
                    error = json.loads(stdout)
                    if (isinstance(error, dict) and error.get("retryable") is False
                            and error.get("error_code") in {
                                "invalid_request", "input_limit", "output_limit",
                                "resource_limit", "worker_failed",
                            }):
                        code = error["error_code"]
                except (ValueError, UnicodeError):
                    pass
                raise AnalysisExecutionError(code)
            result = self._validate_output(stdout, normalized)
            result["execution"] = {
                "backend": self.backend,
                "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
                "input_sha256": hashlib.sha256(payload).hexdigest(),
                "limits": self.description()["resource_limits"],
            }
            return result

    async def _read_bounded(self, stream: asyncio.StreamReader) -> bytes:
        content = bytearray()
        while True:
            chunk = await stream.read(min(4096, self.max_output_bytes + 1))
            if not chunk:
                return bytes(content)
            content.extend(chunk)
            if len(content) > self.max_output_bytes:
                raise AnalysisExecutionError("output_limit")

    async def _exchange(self, process: asyncio.subprocess.Process, payload: bytes) -> tuple:
        async def send() -> None:
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

        tasks = [
            asyncio.create_task(self._read_bounded(process.stdout)),
            asyncio.create_task(self._read_bounded(process.stderr)),
            asyncio.create_task(send()),
            asyncio.create_task(process.wait()),
        ]
        try:
            stdout, stderr, _, returncode = await asyncio.gather(*tasks)
            return stdout, stderr, returncode
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _kill_wait(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

    async def _cleanup_docker(self, docker: str, name: str, cwd: str) -> None:
        cleanup = None
        try:
            cleanup = await asyncio.create_subprocess_exec(
                docker, "--config", cwd, "rm", "--force", name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=cwd, env=dict(_ENV),
            )
            await asyncio.wait_for(cleanup.wait(), timeout=2)
        except (OSError, asyncio.TimeoutError):
            # A removed container also returns nonzero. Never expose daemon
            # output or retry container creation to recover from a failure.
            pass
        finally:
            if cleanup is not None and cleanup.returncode is None:
                await self._kill_wait(cleanup)

    async def _execute(self, payload: bytes) -> tuple[bytes, bytes, int]:
        docker = shutil.which("docker") if self.backend == "docker" else None
        if self.backend == "docker" and docker is None:
            raise AnalysisExecutionError("docker_unavailable")
        name = f"news-analysis-{uuid4().hex}"
        process = None
        success = False
        with tempfile.TemporaryDirectory(prefix="news-analysis-") as cwd:
            worker_args = [
                "--memory-mb", str(self.memory_mb),
                "--cpu-seconds", str(math.ceil(self.timeout_seconds)),
            ]
            if self.backend == "docker":
                command = [
                    docker, "--config", cwd, "run", "--interactive", "--rm",
                    "--name", name, "--pull", "never", "--network", "none",
                    "--read-only", "--cap-drop", "ALL", "--security-opt",
                    "no-new-privileges", "--pids-limit", "32", "--memory",
                    f"{self.memory_mb}m", "--cpus", "1", "--user", "65534:65534",
                    "--log-driver", "none", self.docker_image,
                    "python", "-I", "-S", "/opt/news-analysis/worker.py", *worker_args,
                ]
            else:
                command = [sys.executable, "-I", "-S", str(_WORKER), *worker_args]

            async def start_and_exchange() -> tuple:
                nonlocal process
                spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                    *command, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=cwd, env=dict(_ENV), start_new_session=True,
                    limit=min(65536, self.max_output_bytes + 1),
                ))
                try:
                    # Cancellation can arrive after the OS creates the child but
                    # before asyncio returns its handle. Collect that handle so
                    # the finally block can always kill and reap the child.
                    process = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    try:
                        process = await spawning
                    except OSError:
                        pass
                    raise
                return await self._exchange(process, payload)

            try:
                output = await asyncio.wait_for(
                    start_and_exchange(), timeout=self.timeout_seconds,
                )
                success = output[2] == 0
                return output
            except asyncio.TimeoutError:
                raise AnalysisExecutionError("timeout") from None
            except OSError:
                code = "docker_unavailable" if self.backend == "docker" else "worker_unavailable"
                raise AnalysisExecutionError(code) from None
            finally:
                if process is not None:
                    await self._kill_wait(process)
                if self.backend == "docker" and not success:
                    await asyncio.shield(self._cleanup_docker(docker, name, cwd))

    def _validate_output(self, stdout: bytes, request: dict) -> dict:
        """Verify every core field against the approved bounded calculation.

        Worker/service responses are untrusted, including numeric findings and
        explanatory limitations. Recompute once with the same engine and fixed
        Decimal context, avoiding independent formulas or permissive shape-only
        checks. Canonical JSON also distinguishes bool from integer and retains
        the exact nullable, scope, version and evidence contracts.
        """
        try:
            result = json.loads(
                stdout.decode("utf-8"), parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
            if not isinstance(result, dict):
                raise ValueError
            expected = analyze(request)
            if _json_bytes(result) != _json_bytes(expected):
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise AnalysisExecutionError("invalid_output") from None
