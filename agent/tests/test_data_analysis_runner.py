"""Real fixed-worker execution plus fail-closed lifecycle and container guards."""

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from app.data_analysis.engine import analyze, validate_request
from app.data_analysis.runner import AnalysisExecutionError, AnalysisRunner
import app.data_analysis.runner as runner_module


def request(operation="overview") -> dict:
    return {
        "schema_version": "1.0", "operation": operation, "metric": "ctr",
        "dataset": {
            "run_id": "11111111-1111-4111-8111-111111111111",
            "window_start": "2026-10-03T10:00:00+08:00",
            "window_end": "2026-10-03T11:00:00+08:00",
            "bundle_version": "bundle-v1",
            "rows": [{
                "news_id": "news-1", "rank": 1, "content_type": "article",
                "impressions": 100, "clicks": 10, "unique_users": 7,
                "total_duration_seconds": 140, "effective_consumptions": 7,
                "interactions": 3, "ctr": "0.1000", "hot_score": "0.5",
            }],
        },
    }


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["overview", "distribution", "compare", "quality"])
async def test_real_isolated_worker_matches_deterministic_engine(operation) -> None:
    data = request(operation)
    result = await AnalysisRunner().run(data)
    expected = analyze(data)
    assert {key: value for key, value in result.items() if key != "execution"} == expected
    assert result["execution"]["backend"] == "process"
    assert result["execution"]["input_sha256"] == hashlib.sha256(encoded(validate_request(data))).hexdigest()
    assert result["execution"]["elapsed_ms"] >= 0
    assert result["execution"]["limits"]["os_resource_limits_enforced"] == sys.platform.startswith("linux")
    assert data["dataset"]["rows"][0]["ctr"] == "0.1000"


@pytest.mark.asyncio
async def test_process_has_empty_cwd_clean_environment_and_isolated_python(monkeypatch) -> None:
    monkeypatch.setenv("ENTERPRISE_API_KEY", "test-secret-do-not-forward")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/modules")
    captured = {}
    create = asyncio.create_subprocess_exec

    async def inspect(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        assert list(Path(kwargs["cwd"]).iterdir()) == []
        return await create(*args, **kwargs)

    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", inspect)
    result = await AnalysisRunner().run(request())
    assert captured["args"][:3] == (sys.executable, "-I", "-S")
    assert Path(captured["args"][3]).name == "worker.py"
    assert captured["kwargs"]["env"] == {"LANG": "C.UTF-8"}
    assert not Path(captured["kwargs"]["cwd"]).exists()
    assert "test-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_parent_rejects_code_paths_and_limits_before_execution(monkeypatch) -> None:
    called = False

    async def forbidden(_payload):
        nonlocal called
        called = True
        raise AssertionError("worker must not start")

    runner = AnalysisRunner()
    monkeypatch.setattr(runner, "_execute", forbidden)
    for field in ("code", "script", "path", "sql", "model_prompt"):
        data = request()
        data[field] = "__import__('os').system('echo secret')"
        with pytest.raises(AnalysisExecutionError) as error:
            await runner.run(data)
        assert error.value.error_code == "invalid_request"
        assert error.value.retryable is False
        assert "secret" not in str(error.value)
    runner.max_rows = 1
    data = request()
    data["dataset"]["rows"].append(deepcopy(data["dataset"]["rows"][0]))
    with pytest.raises(AnalysisExecutionError) as error:
        await runner.run(data)
    assert error.value.error_code == "row_limit"
    runner.max_input_bytes = 10
    with pytest.raises(AnalysisExecutionError) as error:
        await runner.run(request())
    assert error.value.error_code == "input_limit"
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["version", "algorithm", "operation", "metric", "hash", "identity", "scope", "analysis", "duplicate"])
async def test_output_is_checked_against_request_not_trusted(monkeypatch, tamper) -> None:
    data = request()
    result = analyze(data)
    if tamper == "version":
        result["schema_version"] = "99"
    elif tamper == "algorithm":
        result["algorithm_version"] = "unknown"
    elif tamper in {"operation", "metric"}:
        result[tamper] = "unrequested"
    elif tamper == "hash":
        result["source"]["snapshot_sha256"] = "0" * 64
    elif tamper == "identity":
        result["source"]["run_id"] = "another-run"
    elif tamper == "scope":
        result["source"]["scope"] = "all_users"
    elif tamper == "analysis":
        result["analysis"]["totals"]["impressions"] = "1; execute command"
    raw = encoded(result)
    if tamper == "duplicate":
        raw = raw[:-1] + b',"schema_version":"1.0"}'

    async def execute(_payload):
        return raw, b"", 0

    runner = AnalysisRunner()
    monkeypatch.setattr(runner, "_execute", execute)
    with pytest.raises(AnalysisExecutionError) as error:
        await runner.run(data)
    assert error.value.error_code == "invalid_output"


class FakeStdin:
    def __init__(self):
        self.content = b""
        self.closed = False

    def write(self, content):
        self.content += content

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class FakeProcess:
    def __init__(self, *, stdout=b"", stderr=b"", code=0, hang=False):
        self.stdin = FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.returncode = None
        self.code = code
        self.killed = False
        self.wait_count = 0
        self.done = asyncio.Event()
        if not hang:
            self.done.set()

    async def wait(self):
        self.wait_count += 1
        await self.done.wait()
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.done.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow_stream", ["stdout", "stderr"])
async def test_both_output_streams_are_bounded_and_process_reaped(monkeypatch, overflow_stream) -> None:
    process = FakeProcess(**{overflow_stream: b"x" * 1025}, hang=True)

    async def create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", create)
    runner = AnalysisRunner(max_output_bytes=1024)
    with pytest.raises(AnalysisExecutionError) as error:
        await runner.run(request())
    assert error.value.error_code == "output_limit"
    assert process.killed
    assert process.wait_count >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_timeout_and_cancellation_kill_wait_and_remove_temp_directory(monkeypatch, failure) -> None:
    process = FakeProcess(hang=True)
    created = asyncio.Event()
    paths = []

    async def create(*_args, **kwargs):
        paths.append(kwargs["cwd"])
        created.set()
        return process

    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", create)
    runner = AnalysisRunner(timeout_seconds=0.01 if failure == "timeout" else 3)
    task = asyncio.create_task(runner.run(request()))
    await created.wait()
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(AnalysisExecutionError) as error:
            await task
        assert error.value.error_code == "timeout"
    assert process.killed
    assert process.wait_count >= 1
    assert all(not Path(path).exists() for path in paths)


@pytest.mark.asyncio
async def test_cancellation_during_process_creation_collects_and_reaps_handle(monkeypatch) -> None:
    process = FakeProcess(hang=True)
    creating = asyncio.Event()
    allow_handle = asyncio.Event()
    paths = []

    async def create(*_args, **kwargs):
        paths.append(kwargs["cwd"])
        creating.set()
        await allow_handle.wait()
        return process

    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(AnalysisRunner().run(request()))
    await creating.wait()
    task.cancel()
    allow_handle.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed
    assert process.wait_count >= 1
    assert all(not Path(path).exists() for path in paths)


@pytest.mark.asyncio
async def test_concurrency_limit_applies_and_slots_are_released(monkeypatch) -> None:
    runner = AnalysisRunner(max_concurrency=2)
    active = 0
    peak = 0

    async def execute(payload):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return encoded(analyze(json.loads(payload))), b"", 0

    monkeypatch.setattr(runner, "_execute", execute)
    results = await asyncio.gather(*(runner.run(request()) for _ in range(7)))
    assert len(results) == 7
    assert peak == 2
    assert active == 0


@pytest.mark.asyncio
async def test_docker_unavailable_fails_closed_without_process_fallback(monkeypatch) -> None:
    monkeypatch.setattr(runner_module.shutil, "which", lambda _command: None)
    with pytest.raises(AnalysisExecutionError) as error:
        await AnalysisRunner(backend="docker").run(request())
    assert error.value.error_code == "docker_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit", "timeout", "cancel"])
async def test_docker_restrictions_and_failure_cleanup_are_explicit(monkeypatch, failure) -> None:
    monkeypatch.setattr(runner_module.shutil, "which", lambda _command: "/test/docker")
    main = FakeProcess(code=125, stderr=b"secret daemon details", hang=failure != "exit")
    commands = []
    created = asyncio.Event()

    async def create(*args, **kwargs):
        commands.append((args, kwargs))
        assert kwargs["env"] == {"LANG": "C.UTF-8"}
        if "run" in args:
            created.set()
            return main
        return FakeProcess()

    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", create)
    runner = AnalysisRunner(backend="docker", timeout_seconds=0.01 if failure == "timeout" else 3)
    task = asyncio.create_task(runner.run(request()))
    await created.wait()
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(AnalysisExecutionError) as error:
            await task
        assert error.value.error_code == ("timeout" if failure == "timeout" else "docker_failed")
        assert "secret" not in str(error.value)
    run_args = commands[0][0]
    flags = dict(zip(run_args, run_args[1:]))
    assert flags["--pull"] == "never"
    assert flags["--network"] == "none"
    assert "--read-only" in run_args
    assert flags["--cap-drop"] == "ALL"
    assert flags["--security-opt"] == "no-new-privileges"
    assert flags["--pids-limit"] == "32"
    assert flags["--memory"] == "128m"
    assert flags["--cpus"] == "1"
    assert flags["--user"] == "65534:65534"
    assert flags["--log-driver"] == "none"
    assert not {"--mount", "--volume", "-v", "--privileged"}.intersection(run_args)
    assert "sh" not in run_args and "bash" not in run_args
    cleanup_args = commands[-1][0]
    assert cleanup_args[-3:-1] == ("rm", "--force")
    assert cleanup_args[-1] == flags["--name"]
    assert len(commands) == 2
    assert not Path(commands[0][1]["cwd"]).exists()
    if failure != "exit":
        assert main.killed


@pytest.mark.asyncio
async def test_real_worker_rejects_invalid_json_without_input_or_exception_details() -> None:
    worker = Path(runner_module.__file__).with_name("worker.py")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-S", str(worker),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env={"LANG": "C.UTF-8"},
    )
    stdout, stderr = await process.communicate(b'{"private_input":"do-not-echo", "broken":')
    assert process.returncode != 0
    assert json.loads(stdout) == {"error_code": "invalid_request", "retryable": False}
    assert b"do-not-echo" not in stdout + stderr
    assert stderr == b""


@pytest.mark.asyncio
async def test_real_worker_hard_input_limit() -> None:
    worker = Path(runner_module.__file__).with_name("worker.py")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-S", str(worker),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env={"LANG": "C.UTF-8"},
    )
    stdout, stderr = await process.communicate(b"x" * 1_048_577)
    assert process.returncode != 0
    assert json.loads(stdout) == {"error_code": "input_limit", "retryable": False}
    assert stderr == b""


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("NEWSAGENT_ANALYSIS_DOCKER_E2E") != "1", reason="opt-in local Docker analysis acceptance")
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["overview", "distribution", "compare", "quality", "baseline", "trend"])
async def test_real_docker_analysis_contract(operation) -> None:
    if operation in {"baseline", "trend"}:
        from tests.test_data_analysis_output_integrity import request as comparison_request
        data = comparison_request(operation)
    else:
        data = request(operation)
    result = await AnalysisRunner(backend="docker", timeout_seconds=5).run(data)
    assert {key: value for key, value in result.items() if key != "execution"} == analyze(data)
    assert result["execution"]["backend"] == "docker"
    assert result["execution"]["limits"]["os_resource_limits_enforced"] is True
