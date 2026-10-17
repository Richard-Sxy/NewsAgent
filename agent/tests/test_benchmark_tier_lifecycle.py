"""HTTP benchmark contracts, concurrency, redaction and failure accounting offline."""
import asyncio
from dataclasses import replace
import hashlib
import json

import httpx
import pytest

from tools.benchmark_tier_lifecycle import (
    BenchmarkConfig, MAX_CONCURRENCY, MAX_REQUESTS, load_queries, main,
    percentiles, run_benchmark, sanitize_trace, search_url,
)


TENANT = "11111111-1111-4111-8111-111111111111"
TOKEN = "secret-benchmark-token"
PRIVATE_QUERY = "企业测试问题不应写入报告"
PRIVATE_ARTICLE = "企业召回正文不应写入报告"


@pytest.fixture
def config(tmp_path):
    source = tmp_path / "questions.jsonl"
    source.write_text(json.dumps({"id": "sensitive-id", "query": PRIVATE_QUERY, "limit": 2})+"\n")
    return BenchmarkConfig(url="http://127.0.0.1:8000", tenant_id=TENANT,
        token_env="BENCHMARK_TOKEN", query_file=source, requests=4, concurrency=2,
        warmup=1, timeout=1.0, output_dir=tmp_path / "evidence")


def samples(config):
    return [json.loads(line) for line in (config.output_dir / "samples.jsonl").read_text().splitlines()]


def payload(total=10.0):
    return {"items": [{"title": PRIVATE_ARTICLE, "excerpt": PRIVATE_ARTICLE}],
        "trace": {"trace_schema_version": 2, "status": "complete", "stage": "complete",
            "policy_version": PRIVATE_QUERY, "total_ms": total,
            "queue_ms": 0.5, "embedding_ms": 1.5, "returned_news": 1,
            "tiers": {tier: {"vector_search_ms": total/2, "state_gate_ms": total/4,
                "filter_ms": 0.1, "search_and_gate_ms": total*.8, "hits": 1,
                "refill_rounds": 0, "components_available": True, "status": "complete",
                "rounds": [{"requested": 2, "retrieved": 1, "accepted": 1,
                            "vector_search_ms": total/2, "state_gate_ms": total/4}]}
                for tier in ("hot", "warm", "cold")}}}


@pytest.mark.asyncio
async def test_shared_client_bounded_concurrency_redaction_and_round_robin(config):
    queries = [{"id": f"private-id-{i}", "query": f"{PRIVATE_QUERY}{i}", "limit": i+1} for i in range(3)]
    config.query_file.write_text("\n".join(json.dumps(item) for item in queries))
    active = peak = 0
    received = []

    async def handler(request):
        nonlocal active, peak
        assert request.method == "POST"
        assert request.url.path == "/api/v1/knowledge/search"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["X-Tenant-ID"] == TENANT
        received.append(json.loads(request.content))
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.005)
        active -= 1
        return httpx.Response(200, json=payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_benchmark(config, client=client, environ={"BENCHMARK_TOKEN": TOKEN})
        assert not client.is_closed
    assert peak == config.concurrency
    assert received == [{"query": queries[i % 3]["query"], "limit": queries[i % 3]["limit"]}
                        for i in [0, 0, 1, 2, 3]]
    assert report["measurement"]["total"] == 4
    assert report["measurement"]["succeeded"] == 4
    assert report["warmup"]["total"] == 1
    assert report["warmup"]["included_in_percentiles"] is False
    assert report["latency_ms"]["server_total_ms"]["count"] == 4
    assert report["tiers"]["hot"]["vector_search_ms"]["p50"] == 5.0
    assert len(samples(config)) == 5
    manifest = json.loads((config.output_dir / "manifest.json").read_text())
    assert manifest["query_file_sha256"] == hashlib.sha256(config.query_file.read_bytes()).hexdigest()
    assert manifest["url"] == "http://127.0.0.1:8000/api/v1/knowledge/search"
    assert manifest["retries"] == 0
    assert manifest["follow_redirects"] is False
    for path in config.output_dir.iterdir():
        saved = path.read_text()
        assert TOKEN not in saved
        assert PRIVATE_QUERY not in saved
        assert PRIVATE_ARTICLE not in saved
        assert "private-id" not in saved


@pytest.mark.asyncio
async def test_failure_trace_and_no_retry_or_redirect_and_success_only_percentiles(config):
    config = replace(config, requests=6, warmup=0, concurrency=1)
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"detail": {"code": PRIVATE_QUERY,
                "trace": {"total_ms": 999, "status": "failed", "tiers": {}}}})
        if calls == 2:
            raise httpx.ConnectError(TOKEN+PRIVATE_QUERY, request=request)
        if calls == 3:
            return httpx.Response(302, headers={"location": "https://not-contacted.invalid"})
        if calls == 4:
            return httpx.Response(200, content=PRIVATE_ARTICLE.encode())
        if calls == 5:
            return httpx.Response(200, json={"items": {}, "trace": {"total_ms": 888}})
        return httpx.Response(200, json=payload(20.0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        report = await run_benchmark(config, client=client, environ={"BENCHMARK_TOKEN": TOKEN})
    assert calls == 6
    assert report["status"] == "failed"
    assert report["measurement"]["failed"] == 5
    assert report["measurement"]["error_rate"] == 5/6
    assert report["measurement"]["error_type_counts"] == {
        "HTTPStatusError": 2, "ConnectError": 1, "InvalidResponse": 2}
    assert report["latency_ms"]["server_total_ms"] == {"count": 1, "p50": 20, "p95": 20, "p99": 20}
    assert samples(config)[0]["trace"]["total_ms"] == 999
    assert samples(config)[1]["http_status"] is None
    assert TOKEN not in (config.output_dir / "samples.jsonl").read_text()


@pytest.mark.asyncio
async def test_whole_request_timeout_does_not_abort_other_requests(config):
    config = replace(config, requests=2, warmup=0, timeout=.01, concurrency=1)
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(.1)
        return httpx.Response(200, json=payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_benchmark(config, client=client, environ={"BENCHMARK_TOKEN": TOKEN})
    assert calls == 2
    assert report["measurement"]["failed"] == 1
    assert report["measurement"]["succeeded"] == 1
    assert samples(config)[0]["error_type"] == "ClientTimeout"


@pytest.mark.asyncio
async def test_warmup_excluded_percentiles_missing_metrics_are_null(config):
    config = replace(config, requests=3, warmup=1, concurrency=1)
    timings = iter([9999, 10, 20, 30])

    async def handler(request):
        value = next(timings)
        return httpx.Response(200, json={"items": [], "trace": {"total_ms": value,
            "tiers": {"hot": {"search_and_gate_ms": value/2}}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_benchmark(config, client=client, environ={"BENCHMARK_TOKEN": TOKEN})
    metric = report["latency_ms"]["server_total_ms"]
    assert metric["count"] == 3 and metric["p50"] == 20
    assert metric["p95"] == pytest.approx(29.0)
    assert metric["p99"] == pytest.approx(29.8)
    assert report["tiers"]["hot"]["vector_search_ms"] == {
        "count": 0, "p50": None, "p95": None, "p99": None}
    assert report["tiers"]["warm"]["search_and_gate_ms"]["p50"] is None
    assert samples(config)[1]["trace"]["tiers"]["hot"]["vector_search_ms"] is None


@pytest.mark.asyncio
async def test_all_failures_produce_empty_latency_population(config):
    config = replace(config, warmup=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"detail": TOKEN}))) as client:
        report = await run_benchmark(config, client=client, environ={"BENCHMARK_TOKEN": TOKEN})
    assert report["measurement"]["failed"] == config.requests
    assert report["measurement"]["error_rate"] == 1
    assert report["latency_ms"]["client_wall_ms"]["p50"] is None
    assert report["latency_ms"]["server_total_ms"]["count"] == 0


@pytest.mark.parametrize("url", [
    "http://localhost:8000", "http://127.0.0.1:8000/", "http://10.2.3.4:8080",
    "http://192.168.2.3", "http://[::1]:8000", "https://enterprise.example",
    "https://enterprise.example/api/v1/knowledge/search",
])
def test_url_accepts_explicit_search_endpoint(url):
    assert search_url(url).endswith("/api/v1/knowledge/search")


@pytest.mark.parametrize("url", [
    "http://public.example", "http://8.8.8.8", "https://user:secret@example.com",
    "https://example.com?token=secret", "https://example.com#secret", "https://example.com?",
    "https://example.com#", "file:///tmp/input", "https://example.com/api/v1/knowledge/documents",
    "https://example.com:bad", " https://example.com", "https://example.com\n",
])
def test_url_rejects_credentials_and_non_search_targets_without_echo(url):
    with pytest.raises(ValueError) as caught:
        search_url(url)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("changes", [
    {"requests": 0}, {"requests": MAX_REQUESTS+1}, {"requests": True},
    {"concurrency": 0}, {"concurrency": MAX_CONCURRENCY+1},
    {"warmup": -1}, {"warmup": 10_001}, {"timeout": float("inf")},
    {"timeout": 0}, {"timeout": 301}, {"tenant_id": "bad"}, {"token_env": "bad-name"},
])
def test_parameters_are_bounded(config, changes):
    with pytest.raises(ValueError):
        replace(config, **changes).validated()


@pytest.mark.parametrize("content", [
    "", '{"id":"a","query":"private","limit":true}',
    '{"id":"a","query":"private","limit":1,"unknown":"private"}',
    '{"id":"a","query":"private","limit":0}',
    '{"id":"a","query":"private","limit":1,"limit":2}',
    '{"id":"a","query":"private","limit":1}\n{"id":"a","query":"private","limit":1}',
    '{"id":"a","query":" ","limit":1}', "private-invalid-json",
])
def test_query_validation_does_not_echo_content(config, content):
    config.query_file.write_text(content)
    with pytest.raises(ValueError) as caught:
        load_queries(config.query_file)
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_missing_token_and_existing_evidence_fail_before_any_request(config):
    with pytest.raises(ValueError, match="token_env"):
        await run_benchmark(config, environ={})
    assert not config.output_dir.exists()
    config.output_dir.mkdir()
    evidence = config.output_dir / "existing.txt"
    evidence.write_text("keep")
    with pytest.raises(ValueError, match="never overwritten"):
        await run_benchmark(config, environ={"BENCHMARK_TOKEN": TOKEN})
    assert evidence.read_text() == "keep"
    assert list(config.output_dir.iterdir()) == [evidence]


def test_trace_allowlist_missing_and_nonfinite_metrics_and_percentile_math():
    result = sanitize_trace({"total_ms": float("nan"), "queue_ms": -1,
        "embedding_ms": True, "final_gate_ms": 10**1000, "stage": PRIVATE_QUERY,
        "trace_schema_version": 2, "tiers": {"hot": {"vector_search_ms": float("inf"),
            "state_gate_ms": 0, "rounds": [{"requested": 10, "text": PRIVATE_QUERY}]*70}},
        "text": PRIVATE_ARTICLE})
    assert result["total_ms"] is None
    assert result["queue_ms"] is None
    assert result["embedding_ms"] is None
    assert result["final_gate_ms"] is None
    assert result["tiers"]["hot"]["state_gate_ms"] == 0
    assert len(result["tiers"]["hot"]["rounds"]) == 64
    assert result["tiers"]["hot"]["rounds_truncated"] is True
    assert PRIVATE_QUERY not in json.dumps(result)
    assert PRIVATE_ARTICLE not in json.dumps(result)
    assert percentiles([10, 20])["p95"] == 19.5
    assert percentiles([]) == {"count": 0, "p50": None, "p95": None, "p99": None}


def test_cli_nonzero_for_failure_and_no_secret_output(config, monkeypatch, capsys):
    monkeypatch.setenv("BENCHMARK_TOKEN", TOKEN)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs:
        httpx.MockTransport(lambda request: httpx.Response(503, json={"detail": TOKEN})))
    args = ["--url", config.url, "--tenant-id", TENANT, "--token-env", "BENCHMARK_TOKEN",
        "--query-file", str(config.query_file), "--requests", "2", "--concurrency", "1",
        "--warmup", "0", "--timeout", "1", "--output-dir", str(config.output_dir)]
    assert main(args) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "failed"
    assert TOKEN not in output.out+output.err
    assert main(args) == 2
    assert "never overwritten" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_warmup_failure_marks_run_failed_without_polluting_measurement(config):
    config = replace(config, requests=1, concurrency=1)
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "unavailable"}) if calls == 1 else httpx.Response(200, json=payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_benchmark(config, client=client, environ={"BENCHMARK_TOKEN": TOKEN})
    assert report["status"] == "failed"
    assert report["warmup"]["failed"] == 1
    assert report["measurement"]["failed"] == 0
    assert report["latency_ms"]["client_wall_ms"]["count"] == 1
