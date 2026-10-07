"""Remote analysis contract, provenance, transport and resource enforcement."""

import asyncio
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from app.data_analysis.engine import analyze, validate_request
from app.data_analysis.remote import RemoteAnalysisRunner
from app.data_analysis.runner import AnalysisExecutionError, AnalysisRunner, _json_bytes


TOKEN = "independent-test-token"
URL = "https://analysis.internal/v1/analyze"


def request(operation="overview"):
    return {
        "schema_version": "1.0", "operation": operation, "metric": "ctr",
        "dataset": {
            "run_id": "11111111-1111-4111-8111-111111111111",
            "window_start": "2026-10-03T10:00:00+08:00",
            "window_end": "2026-10-03T11:00:00+08:00",
            "bundle_version": "bundle-v1", "rows": [{
                "news_id": "news-1", "rank": 1, "content_type": "article",
                "impressions": 100, "clicks": 10, "unique_users": 7,
                "total_duration_seconds": 140, "effective_consumptions": 7,
                "interactions": 3, "ctr": "0.1", "hot_score": "0.5",
            }],
        },
    }


def report(data=None):
    data = validate_request(data or request())
    limits = AnalysisRunner(timeout_seconds=3).description()["resource_limits"]
    limits["os_resource_limits_enforced"] = True
    return {**analyze(data), "execution": {
        "backend": "process", "elapsed_ms": 10,
        "input_sha256": hashlib.sha256(_json_bytes(data)).hexdigest(),
        "limits": limits,
    }}


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


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["overview", "distribution", "compare", "quality"])
async def test_success_canonicalizes_request_and_preserves_service_execution(operation):
    calls = []

    async def handle(submitted):
        calls.append(submitted)
        assert submitted.headers["Authorization"] == "Bearer " + TOKEN
        assert submitted.headers["Accept-Encoding"] == "identity"
        assert submitted.url == URL and submitted.method == "POST"
        assert submitted.content == _json_bytes(validate_request(request(operation)))
        return httpx.Response(200, json=report(request(operation)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        runner = RemoteAnalysisRunner(url=URL, token=TOKEN, client=client)
        output = await runner.run(request(operation))
        assert {key: value for key, value in output.items() if key != "execution"} == analyze(request(operation))
        assert output["execution"]["backend"] == "process"
        assert output["execution"]["transport"] == "service"
        assert runner.description()["execution_backend"] == "service"
        assert "internal" not in str(runner.description()) and TOKEN not in str(runner.description())
        await runner.close()
        assert not client.is_closed
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["trend", "baseline"])
async def test_remote_service_v2_complete_port_to_actual_worker_chain(operation):
    from app.data_analysis.service import AnalysisServiceSettings, create_app

    data = request_v2(operation)
    app = create_app(AnalysisServiceSettings(token=TOKEN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        runner = RemoteAnalysisRunner(
            url="http://analysis.internal/v1/analyze", token=TOKEN,
            allow_insecure_http=True, require_os_limits=False, client=client,
        )
        output = await runner.run(data)
    assert {key: value for key, value in output.items() if key != "execution"} == analyze(data)
    assert output["execution"]["transport"] == "service"


@pytest.mark.asyncio
async def test_reference_rows_share_client_total_row_limit_before_transport():
    calls = 0

    def handle(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=report(request_v2()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AnalysisExecutionError) as error:
            await RemoteAnalysisRunner(url=URL, token=TOKEN, client=client, max_rows=1).run(request_v2())
    assert error.value.error_code == "row_limit" and calls == 0


@pytest.mark.asyncio
async def test_reference_snapshot_hash_tampering_is_rejected():
    data = request_v2()
    output = report(data)
    output["source"]["reference"]["snapshot_sha256"] = "0" * 64
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=output))) as client:
        with pytest.raises(AnalysisExecutionError) as error:
            await RemoteAnalysisRunner(url=URL, token=TOKEN, client=client).run(data)
    assert error.value.error_code == "invalid_output"


@pytest.mark.parametrize("url", [
    "http://analysis.internal/v1/analyze", "ftp://analysis.internal/v1/analyze",
    "https://user:secret@analysis.internal/v1/analyze", "https://analysis.internal/v1/other",
    "https://analysis.internal/v1/analyze?token=secret", "https://analysis.internal/v1/analyze#secret",
    "https://analysis.internal/v1/analyze?", "https://analysis.internal/v1/analyze#",
    "https:///v1/analyze", "https://analysis.internal:0/v1/analyze",
    "https://analysis.internal:wrong/v1/analyze", "https://analysis.internal/v1/analyze\n",
    "https://analysis.internal/v1/analyze/", "https://analysis.internal/v1/%61nalyze",
    "https://analysis.internal\\other.internal/v1/analyze",
])
def test_invalid_configured_urls_are_rejected_safely(url):
    with pytest.raises(ValueError) as error:
        RemoteAnalysisRunner(url=url, token=TOKEN)
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_local_http_requires_explicit_policy():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=report()))) as client:
        runner = RemoteAnalysisRunner(url="http://127.0.0.1:28100/v1/analyze", token=TOKEN, allow_insecure_http=True, client=client)
        assert (await runner.run(request()))["operation"] == "overview"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [
    (401, "service_unauthorized"), (403, "service_unauthorized"),
    (413, "input_limit"), (429, "service_busy"), (504, "timeout"),
    (301, "service_failed"), (307, "service_failed"), (500, "service_failed"),
])
async def test_non_success_is_safe_nonretryable_without_reading_error_body(status, code):
    calls = 0
    stream = Stream([b"secret-token-or-private-body"])

    def handle(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, stream=stream, headers={"Location": "https://other.internal/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AnalysisExecutionError) as error:
            await RemoteAnalysisRunner(url=URL, token=TOKEN, client=client).run(request())
    assert error.value.error_code == code and error.value.retryable is False
    assert "secret" not in str(error.value)
    assert calls == 1 and stream.reads == 0 and stream.closed


@pytest.mark.asyncio
async def test_transport_failure_is_safe_without_retry_or_local_fallback():
    calls = 0

    def handle(submitted):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("private-url-and-secret", request=submitted)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AnalysisExecutionError) as error:
            await RemoteAnalysisRunner(url=URL, token=TOKEN, client=client).run(request())
    assert error.value.error_code == "service_unavailable"
    assert "private" not in str(error.value) and calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_total_deadline_and_cancellation_close_response_stream(failure):
    stream = Stream([b"{"], delay=1)
    entered = asyncio.Event()

    def handle(_request):
        entered.set()
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        runner = RemoteAnalysisRunner(url=URL, token=TOKEN, client=client, timeout_seconds=0.02 if failure == "timeout" else 5)
        task = asyncio.create_task(runner.run(request()))
        await entered.wait()
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(AnalysisExecutionError) as error:
                await task
            assert error.value.error_code == "timeout"
    assert stream.closed


@pytest.mark.asyncio
async def test_chunked_output_is_bounded_before_json_processing():
    stream = Stream([b"x" * 4096 for _ in range(20)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))) as client:
        with pytest.raises(AnalysisExecutionError) as error:
            await RemoteAnalysisRunner(url=URL, token=TOKEN, client=client, max_output_bytes=4096).run(request())
    assert error.value.error_code == "output_limit"
    assert stream.reads == 2 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("length,code", [("300000", "output_limit"), ("broken", "invalid_output"), ("-1", "invalid_output")])
async def test_content_length_bound_rejects_without_stream_read(length, code):
    stream = Stream([b"private"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream, headers={"Content-Length": length}))) as client:
        with pytest.raises(AnalysisExecutionError) as error:
            await RemoteAnalysisRunner(url=URL, token=TOKEN, client=client).run(request())
    assert error.value.error_code == code
    assert stream.reads == 0 and stream.closed


@pytest.mark.asyncio
async def test_compressed_response_is_rejected_before_decoding_or_reading():
    stream = Stream([b"untrusted-compressed-body"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream, headers={"Content-Encoding": "gzip"}))) as client:
        with pytest.raises(AnalysisExecutionError) as error:
            await RemoteAnalysisRunner(url=URL, token=TOKEN, client=client).run(request())
    assert error.value.error_code == "invalid_output"
    assert stream.reads == 0 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    lambda data: data.update(private="do-not-echo"),
    lambda data: data["source"].update(run_id="wrong"),
    lambda data: data["analysis"].update(weighted_ctr="NaN"),
    lambda data: data["execution"].update(backend="arbitrary"),
    lambda data: data["execution"].update(elapsed_ms=True),
    lambda data: data["execution"].update(input_sha256="0" * 64),
    lambda data: data["execution"].update(private="do-not-echo"),
    lambda data: data["execution"]["limits"].update(os_resource_limits_enforced=False),
    lambda data: data["execution"]["limits"].update(timeout_seconds=float("inf")),
    lambda data: data["execution"]["limits"].update(max_concurrency=32),
    lambda data: data["execution"]["limits"].update(max_rows=True),
    lambda data: data["execution"]["limits"].update(max_input_bytes=1),
    lambda data: data["execution"]["limits"].update(max_output_bytes=1),
    lambda data: data["execution"]["limits"].update(memory_mb=4096),
    lambda data: data["execution"]["limits"].update(private="do-not-echo"),
])
async def test_malformed_unbound_or_weaker_reports_fail_closed(change):
    output = report()
    change(output)
    raw = json.dumps(output, allow_nan=True).encode()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=raw))) as client:
        with pytest.raises(AnalysisExecutionError) as error:
            await RemoteAnalysisRunner(url=URL, token=TOKEN, client=client).run(request())
    assert error.value.error_code == "invalid_output" and "do-not-echo" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("undershoot", [0, 1])
async def test_declared_output_limit_binds_exact_wire_report_size(undershoot):
    output = report()
    # Resolve the self-referential byte count: the limit's decimal digits are
    # themselves part of the report. This tests the complete wire report rather
    # than only its core statistics or a different canonical serialization.
    for _ in range(5):
        raw = _json_bytes(output)
        limit = len(raw) - undershoot
        if output["execution"]["limits"]["max_output_bytes"] == limit:
            break
        output["execution"]["limits"]["max_output_bytes"] = limit
    raw = _json_bytes(output)
    assert output["execution"]["limits"]["max_output_bytes"] == len(raw) - undershoot
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=raw))) as client:
        runner = RemoteAnalysisRunner(url=URL, token=TOKEN, client=client)
        if undershoot:
            with pytest.raises(AnalysisExecutionError) as error:
                await runner.run(request())
            assert error.value.error_code == "invalid_output"
        else:
            assert (await runner.run(request()))["operation"] == "overview"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b'{"private":"do-not-echo","private":"do-not-echo"}', b'{"private":NaN}', b"[]"])
async def test_duplicate_keys_and_nonfinite_or_nonobject_response_are_rejected(raw):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=raw))) as client:
        with pytest.raises(AnalysisExecutionError) as error:
            await RemoteAnalysisRunner(url=URL, token=TOKEN, client=client).run(request())
    assert error.value.error_code == "invalid_output"


@pytest.mark.asyncio
async def test_os_limits_may_only_be_relaxed_for_explicit_development_policy():
    output = report()
    output["execution"]["limits"]["os_resource_limits_enforced"] = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=output))) as client:
        runner = RemoteAnalysisRunner(url=URL, token=TOKEN, client=client, require_os_limits=False)
        assert (await runner.run(request()))["execution"]["limits"]["os_resource_limits_enforced"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,code", [("invalid", "invalid_request"), ("bytes", "input_limit"), ("rows", "row_limit")])
async def test_invalid_or_oversized_input_never_reaches_transport(kind, code):
    calls = 0

    def handle(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=report())

    data = request()
    limits = {}
    if kind == "invalid":
        data["private"] = "do-not-echo"
    elif kind == "bytes":
        limits["max_input_bytes"] = 10
    else:
        second = deepcopy(data["dataset"]["rows"][0])
        second.update(news_id="news-2", rank=2)
        data["dataset"]["rows"].append(second)
        limits["max_rows"] = 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AnalysisExecutionError) as error:
            await RemoteAnalysisRunner(url=URL, token=TOKEN, client=client, **limits).run(data)
    assert error.value.error_code == code and calls == 0


@pytest.mark.asyncio
async def test_transport_concurrency_rejects_without_queue_and_cancel_releases_slot():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handle(_request):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        output = report()
        output["execution"]["limits"]["max_concurrency"] = 1
        return httpx.Response(200, json=output)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        runner = RemoteAnalysisRunner(url=URL, token=TOKEN, client=client, max_concurrency=1)
        first = asyncio.create_task(runner.run(request()))
        await started.wait()
        with pytest.raises(AnalysisExecutionError) as error:
            await runner.run(request())
        assert error.value.error_code == "service_busy" and calls == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await runner.run(request()))["operation"] == "overview"
        assert calls == 2


@pytest.mark.asyncio
async def test_owned_client_disables_proxy_redirects_and_closes(monkeypatch):
    captured = {}
    real_client = httpx.AsyncClient

    def build_client(**kwargs):
        captured.update(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr("app.data_analysis.remote.httpx.AsyncClient", build_client)
    runner = RemoteAnalysisRunner(url=URL, token=TOKEN)
    assert captured["trust_env"] is False and captured["follow_redirects"] is False
    await runner.close()
    assert runner._client.is_closed
    with pytest.raises(AnalysisExecutionError) as error:
        await runner.run(request())
    assert error.value.error_code == "service_unavailable"
