"""Opt-in HTTP acceptance against an independently started hardened container.

Uses only public synthetic aggregates. It never reads .env, builds/deploys the
service, prints credentials or connects an enterprise source. Inject a dedicated
test token and set NEWSAGENT_ANALYSIS_SERVICE_DOCKER_E2E=1 to enable.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import os
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import SecretStr
import pytest

from app.data_analysis.engine import analyze, validate_request
from app.data_analysis.remote import RemoteAnalysisRunner
from app.data_analysis.runner import _json_bytes


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEWSAGENT_ANALYSIS_SERVICE_DOCKER_E2E") != "1",
        reason="opt-in independently deployed Docker service acceptance",
    ),
]


@dataclass(frozen=True)
class ServiceConfig:
    url: str
    token: SecretStr
    max_input_bytes: int


@pytest.fixture
def service_config() -> ServiceConfig:
    token = os.environ.get("NEWSAGENT_ANALYSIS_SERVICE_E2E_TOKEN")
    if not token:
        pytest.fail("Inject a dedicated NEWSAGENT_ANALYSIS_SERVICE_E2E_TOKEN without printing it")
    url = os.environ.get(
        "NEWSAGENT_ANALYSIS_SERVICE_E2E_URL", "http://127.0.0.1:28100/v1/analyze",
    )
    # Validate the fixed URL without performing a request or using any proxy.
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or parsed.path != "/v1/analyze"
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "?" in url or "#" in url):
        pytest.fail("Invalid fixed analysis acceptance URL")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Plain HTTP acceptance requires an explicit loopback service")
    return ServiceConfig(
        url=url, token=SecretStr(token),
        max_input_bytes=int(os.environ.get("NEWSAGENT_ANALYSIS_SERVICE_E2E_MAX_INPUT_BYTES", "262144")),
    )


def request(operation: str) -> dict:
    data = {
        "schema_version": "1.0", "operation": operation, "metric": "ctr",
        "dataset": {
            "run_id": "11111111-1111-4111-8111-111111111111",
            "window_start": "2026-10-03T10:00:00+08:00",
            "window_end": "2026-10-03T11:00:00+08:00",
            "bundle_version": "public-synthetic-bundle-v1", "rows": [{
                "news_id": "public-synthetic-news-1", "rank": 1,
                "content_type": "article", "impressions": 100, "clicks": 10,
                "unique_users": 7, "total_duration_seconds": 140,
                "effective_consumptions": 7, "interactions": 3,
                "ctr": "0.1", "hot_score": "0.5",
            }],
        },
    }
    if operation in {"baseline", "trend"}:
        data.update(schema_version="2.0", metric="clicks")
        data["dataset"]["provenance"] = {
            "workflow_version": "public-synthetic-workflow-v1",
            "selection_scope_sha256": "a" * 64,
        }
        if operation == "trend":
            reference = deepcopy(data["dataset"])
            reference.update(
                run_id="22222222-2222-4222-8222-222222222222",
                window_start="2026-10-03T09:00:00+08:00",
                window_end="2026-10-03T10:00:00+08:00",
            )
            reference["rows"][0].update(clicks=5, ctr="0.05")
            data["reference_dataset"] = reference
        else:
            data["dataset"]["rows"][0]["baseline"] = {
                "sample_count": 10, "reference_version": "public-synthetic-baseline-v1",
                "impressions": "100", "clicks": "5", "total_duration_seconds": "80",
                "effective_consumptions": "3", "interactions": "1", "ctr": "0.05",
            }
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["overview", "distribution", "compare", "quality", "baseline", "trend"])
async def test_real_service_six_operations_provenance_hash_and_linux_limits(service_config, operation):
    data = request(operation)
    runner = RemoteAnalysisRunner(
        url=service_config.url, token=service_config.token.get_secret_value(),
        allow_insecure_http=urlsplit(service_config.url).scheme == "http",
        require_os_limits=True,
    )
    try:
        result = await runner.run(data)
    finally:
        await runner.close()
    assert {key: value for key, value in result.items() if key != "execution"} == analyze(data)
    assert result["execution"]["input_sha256"] == hashlib.sha256(_json_bytes(validate_request(data))).hexdigest()
    assert result["execution"]["backend"] == "process"
    assert result["execution"]["transport"] == "service"
    assert result["execution"]["limits"]["os_resource_limits_enforced"] is True


@pytest.mark.asyncio
async def test_real_service_permission_rejection_before_worker(service_config):
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        denied = await client.post(service_config.url, content=_json_bytes(request("overview")))
    assert denied.status_code == 401
    assert denied.json() == {"error_code": "unauthorized", "retryable": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True])
async def test_real_service_rejects_oversized_declared_and_chunked_input(service_config, chunked):
    async def chunks():
        remaining = service_config.max_input_bytes + 1
        while remaining:
            size = min(4096, remaining)
            yield b"x" * size
            remaining -= size

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        result = await client.post(
            service_config.url,
            content=chunks() if chunked else b"x" * (service_config.max_input_bytes + 1),
            headers={"Authorization": "Bearer " + service_config.token.get_secret_value(),
                     "Content-Type": "application/json"},
        )
    assert result.status_code == 413
    assert result.json() == {"error_code": "input_limit", "retryable": False}


@pytest.mark.asyncio
async def test_real_service_health_is_authenticated(service_config):
    parsed = urlsplit(service_config.url)
    health_url = urlunsplit(parsed._replace(path="/health"))
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        denied = await client.get(health_url)
        allowed = await client.get(health_url, headers={
            "Authorization": "Bearer " + service_config.token.get_secret_value(),
        })
    assert denied.status_code == 401
    assert allowed.status_code == 200 and allowed.json() == {"status": "ok"}


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method,status", [
    ("/unknown", "GET", 404), ("/v1/analyze", "GET", 405),
    ("/health", "POST", 405), ("/v1/analyze?unexpected=1", "POST", 404),
    ("/v1/analyze?", "POST", 404),
])
async def test_real_loopback_gateway_rejects_other_routes_methods_and_query(service_config, path, method, status):
    parsed = urlsplit(service_config.url)
    gateway_url = urlunsplit(parsed._replace(path="", query="", fragment="")) + path
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        result = await client.request(method, gateway_url, headers={
            "Authorization": "Bearer " + service_config.token.get_secret_value(),
        })
    assert result.status_code == status


@pytest.mark.asyncio
async def test_real_loopback_gateway_streams_incomplete_body_into_service_total_deadline(service_config):
    parsed = urlsplit(service_config.url)
    if parsed.scheme != "http":
        pytest.skip("development loopback gateway raw HTTP deadline acceptance")
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port or 80)
    try:
        # Send only one body byte and wait. Buffering the entire body in Nginx
        # would miss the service's five-second deadline and fail this bound.
        headers = (
            "POST /v1/analyze HTTP/1.1\r\nHost: analysis-gateway\r\n"
            "Authorization: Bearer " + service_config.token.get_secret_value()
            + "\r\nContent-Type: application/json\r\nContent-Length: 64\r\n"
            "Connection: close\r\n\r\n{"
        )
        writer.write(headers.encode("ascii"))
        await writer.drain()
        response_headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=7)
        assert response_headers.split(b"\r\n", 1)[0].split()[1] == b"504"
    finally:
        writer.close()
        await writer.wait_closed()
