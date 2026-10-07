"""Private service permission, bounded-I/O and worker lifetime contracts."""

import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from app.data_analysis.engine import analyze, validate_request
from app.data_analysis.runner import AnalysisRunner, _json_bytes
from app.data_analysis.service import AnalysisServiceSettings, create_app


TOKEN = "independent-test-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


def request(operation="overview"):
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
                "interactions": 3, "ctr": "0.1", "hot_score": "0.5",
            }],
        },
    }


def config(**changes):
    return AnalysisServiceSettings(token=TOKEN, **changes)


def request_v2(operation="trend"):
    data = request()
    data.update(schema_version="2.0", operation=operation, metric="clicks")
    data["dataset"]["provenance"] = {
        "workflow_version": "workflow-v1", "selection_scope_sha256": "a" * 64,
    }
    if operation == "trend":
        reference = deepcopy(data["dataset"])
        reference.update(
            run_id="22222222-2222-4222-8222-222222222222",
            window_start="2026-10-03T09:00:00+08:00",
            window_end="2026-10-03T10:00:00+08:00",
        )
        reference["rows"][0]["clicks"] = 5
        data["reference_dataset"] = reference
    else:
        data["dataset"]["rows"][0]["baseline"] = {
            "sample_count": 10, "reference_version": "baseline-v1",
            "impressions": "100", "clicks": "5", "total_duration_seconds": "80",
            "effective_consumptions": "3", "interactions": "1", "ctr": "0.05",
        }
    return data


def report(data, settings):
    normalized = validate_request(data)
    runner = AnalysisRunner(
        timeout_seconds=settings.timeout_seconds, max_rows=settings.max_rows,
        max_input_bytes=settings.max_input_bytes, max_output_bytes=settings.max_output_bytes,
        max_concurrency=settings.max_concurrency, memory_mb=settings.memory_mb,
    )
    return {**analyze(data), "execution": {
        "backend": "process", "elapsed_ms": 1,
        "input_sha256": hashlib.sha256(_json_bytes(normalized)).hexdigest(),
        "limits": runner.description()["resource_limits"],
    }}


class WaitingRunner:
    def __init__(self, settings):
        self.settings = settings
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaned = asyncio.Event()

    async def run(self, data):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
            return report(data, self.settings)
        finally:
            self.cleaned.set()


def asgi_request(app, *, body=None, headers=None):
    queue = asyncio.Queue()
    messages = []
    if body is not None:
        queue.put_nowait({"type": "http.request", "body": body, "more_body": False})
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/v1/analyze",
        "raw_path": b"/v1/analyze", "query_string": b"", "root_path": "",
        "headers": [(b"authorization", ("Bearer " + TOKEN).encode())] if headers is None else headers,
        "client": ("127.0.0.1", 1234), "server": ("analysis-service", 8100),
    }

    async def send(message):
        messages.append(message)

    return SimpleNamespace(
        task=asyncio.create_task(app(scope, queue.get, send)), queue=queue,
        messages=messages,
    )


def response(messages):
    return (
        next(message["status"] for message in messages if message["type"] == "http.response.start"),
        json.loads(b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong-test-token"},
    {"Authorization": "bearer " + TOKEN},
    [("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)],
])
async def test_auth_rejected_before_body_or_worker(headers):
    settings = config()
    runner = WaitingRunner(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, runner)), base_url="http://service") as client:
        result = await client.post("/v1/analyze", content=b"x" * 1_048_577, headers=headers)
    assert result.status_code == 401
    assert result.json() == {"error_code": "unauthorized", "retryable": False}
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_health_requires_token_and_docs_are_disabled():
    settings = config()
    runner = WaitingRunner(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, runner)), base_url="http://service") as client:
        assert (await client.get("/health")).status_code == 401
        assert (await client.get("/health", headers=AUTH)).json() == {"status": "ok"}
        assert (await client.get("/docs", headers=AUTH)).status_code == 404
    assert runner.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    b'{"operation":"overview","operation":"overview"}',
    b'{"dataset":{"private":"secret","private":"secret"}}',
    b'{"dataset":NaN}', b'{"dataset":Infinity}',
    json.dumps({**request(), "untrusted_instruction": "private-do-not-echo"}).encode(),
])
async def test_untrusted_json_is_rejected_before_worker_without_echo(raw):
    settings = config()
    runner = WaitingRunner(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, runner)), base_url="http://service") as client:
        result = await client.post("/v1/analyze", content=raw, headers=AUTH)
    assert result.status_code == 400
    assert result.json() == {"error_code": "invalid_request", "retryable": False}
    assert "private" not in result.text and "secret" not in result.text
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_chunked_body_is_bounded_during_collection():
    settings = config(max_input_bytes=10)
    runner = WaitingRunner(settings)
    seen = 0

    async def chunks():
        nonlocal seen
        for _ in range(100):
            seen += 1
            yield b"123456"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, runner)), base_url="http://service") as client:
        result = await client.post("/v1/analyze", content=chunks(), headers=AUTH)
    assert result.status_code == 413
    assert result.json()["error_code"] == "input_limit"
    assert seen == 2 and runner.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["overview", "distribution", "compare", "quality"])
async def test_service_runs_real_fixed_worker(operation):
    settings = config()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://service") as client:
        result = await client.post("/v1/analyze", json=request(operation), headers=AUTH)
    assert result.status_code == 200
    output = result.json()
    assert {key: value for key, value in output.items() if key != "execution"} == analyze(request(operation))
    assert output["execution"]["backend"] == "process"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["trend", "baseline"])
async def test_service_runs_v2_comparison_in_actual_fixed_worker(operation):
    data = request_v2(operation)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config())), base_url="http://service") as client:
        result = await client.post("/v1/analyze", json=data, headers=AUTH)
    assert result.status_code == 200
    output = result.json()
    assert {key: value for key, value in output.items() if key != "execution"} == analyze(data)
    assert output["schema_version"] == "2.0"
    if operation == "trend":
        assert output["source"]["reference"]["row_count"] == 1


@pytest.mark.asyncio
async def test_reference_rows_share_the_service_total_row_limit():
    settings = config(max_rows=1)
    runner = WaitingRunner(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, runner)), base_url="http://service") as client:
        result = await client.post("/v1/analyze", json=request_v2(), headers=AUTH)
    assert result.status_code == 413 and result.json()["error_code"] == "row_limit"
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_concurrency_saturation_rejects_without_queue_and_releases_slot():
    settings = config(max_concurrency=1)
    runner = WaitingRunner(settings)
    app = create_app(settings, runner)
    first = asgi_request(app, body=_json_bytes(request()))
    await asyncio.wait_for(runner.started.wait(), 1)
    second = asgi_request(app, body=_json_bytes(request()))
    await asyncio.wait_for(second.task, 1)
    assert response(second.messages) == (429, {"error_code": "service_busy", "retryable": False})
    assert runner.calls == 1
    runner.release.set()
    await first.task
    assert response(first.messages)[0] == 200
    third = asgi_request(app, body=_json_bytes(request()))
    await third.task
    assert response(third.messages)[0] == 200 and runner.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["body", "worker"])
async def test_total_timeout_covers_body_and_execution_and_cleans_worker(phase):
    settings = config(timeout_seconds=0.02, request_timeout_seconds=0.03)
    runner = WaitingRunner(settings)
    session = asgi_request(create_app(settings, runner), body=_json_bytes(request()) if phase == "worker" else None)
    await asyncio.wait_for(session.task, 1)
    assert response(session.messages) == (504, {"error_code": "timeout", "retryable": False})
    if phase == "worker":
        assert runner.cleaned.is_set()
    else:
        assert runner.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disconnect", "cancel", "shutdown"])
async def test_disconnect_cancel_shutdown_reach_worker_cleanup(reason):
    settings = config()
    runner = WaitingRunner(settings)
    app = create_app(settings, runner)
    async with app.router.lifespan_context(app):
        session = asgi_request(app, body=_json_bytes(request()))
        await asyncio.wait_for(runner.started.wait(), 1)
        if reason == "disconnect":
            session.queue.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(session.task, 1)
            assert response(session.messages)[0] == 499
        elif reason == "cancel":
            session.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await session.task
    if reason == "shutdown":
        with pytest.raises(asyncio.CancelledError):
            await session.task
    assert runner.cleaned.is_set()


@pytest.mark.asyncio
async def test_invalid_worker_output_fails_closed(monkeypatch):
    runner = AnalysisRunner()

    async def execute(_payload):
        return b'{"private-worker-output":"do-not-echo"}', b"", 0

    monkeypatch.setattr(runner, "_execute", execute)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config(), runner)), base_url="http://service") as client:
        result = await client.post("/v1/analyze", json=request(), headers=AUTH)
    assert result.status_code == 502
    assert result.json() == {"error_code": "invalid_output", "retryable": False}
    assert "private" not in result.text


@pytest.mark.asyncio
async def test_injected_port_cannot_bypass_output_contract():
    class InvalidPort:
        async def run(self, _data):
            return {"private": "do-not-echo"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config(), InvalidPort())), base_url="http://service") as client:
        result = await client.post("/v1/analyze", json=request(), headers=AUTH)
    assert result.status_code == 502 and result.json()["error_code"] == "invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disconnect", "cancel", "timeout", "shutdown"])
async def test_service_lifetime_reaps_actual_worker_process(monkeypatch, reason):
    settings = config(timeout_seconds=0.02, request_timeout_seconds=0.04) if reason == "timeout" else config()
    runner = AnalysisRunner(timeout_seconds=settings.timeout_seconds)
    started = asyncio.Event()
    children = []

    async def hold_exchange(process, _payload):
        # The real fixed worker blocks on stdin; service cancellation must reach
        # AnalysisRunner's finally path and kill/reap this OS process.
        children.append(process)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "_exchange", hold_exchange)
    app = create_app(settings, runner)
    async with app.router.lifespan_context(app):
        session = asgi_request(app, body=_json_bytes(request()))
        await asyncio.wait_for(started.wait(), 1)
        if reason == "disconnect":
            session.queue.put_nowait({"type": "http.disconnect"})
            await session.task
            assert response(session.messages)[0] == 499
        elif reason == "cancel":
            session.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await session.task
        elif reason == "timeout":
            await session.task
            assert response(session.messages)[0] == 504
    if reason == "shutdown":
        with pytest.raises(asyncio.CancelledError):
            await session.task
    assert len(children) == 1
    assert children[0].returncode is not None


@pytest.mark.asyncio
async def test_repeated_http_cancellation_cannot_interrupt_worker_cleanup():
    started = asyncio.Event()
    cleaning = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleaned = asyncio.Event()
    cancellation_counts = []

    class SlowCleanup:
        async def run(self, _request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await allow_cleanup.wait()
                cancellation_counts.append(asyncio.current_task().cancelling())
                cleaned.set()

    session = asgi_request(create_app(config(), SlowCleanup()), body=_json_bytes(request()))
    await asyncio.wait_for(started.wait(), 1)
    session.task.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    session.task.cancel()
    await asyncio.sleep(0)
    assert not session.task.done()
    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await session.task
    assert cleaned.is_set() and cancellation_counts == [1]


def test_settings_do_not_read_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANALYSIS_SERVICE_TOKEN=should-not-be-read\n")
    monkeypatch.delenv("ANALYSIS_SERVICE_TOKEN", raising=False)
    with pytest.raises(ValueError):
        AnalysisServiceSettings()
    assert config().model_config["env_file"] is None


def test_production_requires_linux_os_limits(monkeypatch):
    certificates = {"tls_certfile": "/run/analysis-tls/tls.crt", "tls_keyfile": "/run/analysis-tls/tls.key"}
    monkeypatch.setattr("app.data_analysis.service.sys.platform", "darwin")
    with pytest.raises(ValueError, match="Linux"):
        config(environment="production", **certificates)
    monkeypatch.setattr("app.data_analysis.service.sys.platform", "linux")
    assert config(environment="production", **certificates).require_linux_limits is True
    with pytest.raises(ValueError, match="Linux"):
        config(environment="production", require_linux_limits=False, **certificates)


@pytest.mark.parametrize("certificates", [
    {}, {"tls_certfile": "", "tls_keyfile": ""},
    {"tls_certfile": "  ", "tls_keyfile": "  "},
    {"tls_certfile": "/run/analysis-tls/tls.crt"},
    {"tls_keyfile": "/run/analysis-tls/tls.key"},
])
def test_production_rejects_plaintext_or_incomplete_tls_before_start(monkeypatch, certificates):
    monkeypatch.setattr("app.data_analysis.service.sys.platform", "linux")
    with pytest.raises(ValueError, match="TLS"):
        config(environment="production", **certificates)


def test_invalid_configuration_errors_hide_secret_input():
    private = "private-secret-that-must-never-be-echoed"
    with pytest.raises(ValueError) as error:
        AnalysisServiceSettings(token=private, environment="unsupported")
    assert private not in str(error.value)


def test_main_disables_access_logs(monkeypatch):
    from app.data_analysis import service
    import uvicorn

    captured = {}
    settings = AnalysisServiceSettings(token=TOKEN)
    monkeypatch.setattr(service, "AnalysisServiceSettings", lambda: settings)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    service.main()
    assert captured["access_log"] is False
    assert captured["proxy_headers"] is False
    assert captured["workers"] == 1


def test_main_passes_tls_paths_to_server_without_reading_certificates(monkeypatch):
    from app.data_analysis import service
    import uvicorn

    settings = AnalysisServiceSettings(
        token=TOKEN, tls_certfile="/not-read/test.crt", tls_keyfile="/not-read/test.key",
    )
    captured = {}
    monkeypatch.setattr(service, "AnalysisServiceSettings", lambda: settings)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    service.main()
    assert captured["ssl_certfile"] == "/not-read/test.crt"
    assert captured["ssl_keyfile"] == "/not-read/test.key"
