"""Audience transport, data-boundary and isolated-fixture acceptance contracts."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.modules.audience.client import HttpAudienceProfileClient
from app.modules.audience.contracts import (
    AudienceProviderError,
    AudienceQuery,
    AudienceSelection,
    AudienceSnapshot,
    audience_query_fingerprint,
)
from app.modules.audience.factory import build_audience_client
from app.modules.audience.local import LocalAudienceProfileClient
from app.modules.audience.taxonomy import Topic, TopicCatalog


NOW = datetime(2026, 10, 16, 8, tzinfo=UTC)
TENANT = UUID("11111111-1111-4111-8111-111111111111")
RUN = UUID("22222222-2222-4222-8222-222222222222")


@pytest.mark.asyncio
async def test_synthetic_factory_honors_short_freshness_budget():
    settings = SimpleNamespace(audience_backend="synthetic", environment="test",
                               audience_max_snapshot_age_seconds=60)
    client = build_audience_client(settings, catalog())
    try:
        result = await client.estimate(query())
        assert result.expires_at - result.generated_at == timedelta(seconds=60)
    finally:
        await client.close()


def catalog(*, status="draft"):
    return TopicCatalog(taxonomy_version="news-interest-demo-v1", status=status, topics=(
        Topic(topic_id="tech", name="科技", provenance="user_confirmed"),
        Topic(topic_id="tech.nature", name="自然科学", parent_topic_id="tech", provenance="user_confirmed"),
        Topic(topic_id="finance", name="财经", provenance="user_confirmed"),
        Topic(topic_id="tech.disabled", name="停用", parent_topic_id="tech", enabled=False,
              provenance="draft_extension"),
    ))


def query(*, include=("tech.nature",), exclude=(), match="any", **overrides):
    values = dict(tenant_id=TENANT, run_id=RUN, news_id="news-1",
                  window_start=NOW - timedelta(hours=1), window_end=NOW,
                  selection=AudienceSelection(taxonomy_version="news-interest-demo-v1",
                      include_topic_ids=include, exclude_topic_ids=exclude, match=match))
    values.update(overrides)
    return AudienceQuery(**values)


def snapshot_payload(request=None, **overrides):
    request = request or query()
    values = dict(tenant_id=request.tenant_id, run_id=request.run_id, news_id=request.news_id,
        taxonomy_version=request.selection.taxonomy_version, query_fingerprint=audience_query_fingerprint(request),
        snapshot_id="enterprise-snapshot-1", profile_version="profile-v1", source_version="audience-v1",
        window_start=request.window_start, window_end=request.window_end,
        generated_at=NOW, expires_at=NOW + timedelta(minutes=5), watermark=NOW,
        is_complete=True, matched_users=100, eligible_users=80, source_mode="enterprise")
    values.update(overrides)
    return AudienceSnapshot(**values).model_dump(mode="json")


def http_client(handler, **options):
    return HttpAudienceProfileClient(endpoint_url="https://audience.example.invalid/v1/estimate",
        token="PRIVATE_TOKEN", transport=httpx.MockTransport(handler), **options)


def test_selection_is_bounded_immutable_and_canonical():
    first = query(include=("tech", "finance"))
    reordered = query(include=("finance", "tech"))
    assert audience_query_fingerprint(first) == audience_query_fingerprint(reordered)
    assert len(audience_query_fingerprint(first)) == 64
    assert audience_query_fingerprint(first) != audience_query_fingerprint(query(include=("tech",)))
    for include, exclude in [((), ()), (("tech", "tech"), ()), (("tech",), ("tech",))]:
        with pytest.raises(ValidationError):
            query(include=include, exclude=exclude)
    with pytest.raises(ValidationError):
        first.selection.match = "all"


@pytest.mark.parametrize("field,value", [
    ("matched_users", True), ("eligible_users", "80"), ("matched_users", -1),
    ("eligible_users", 101), ("expires_at", NOW.isoformat()),
    ("window_end", (NOW - timedelta(hours=2)).isoformat()), ("is_complete", "true"),
    ("user_id", "private-user"), ("users", ["private-user"]),
])
def test_snapshot_rejects_invalid_aggregates_or_recipient_fields(field, value):
    payload = snapshot_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        AudienceSnapshot.model_validate(payload)


@pytest.mark.asyncio
async def test_http_posts_fixed_query_once_and_returns_only_aggregates():
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.headers["X-Tenant-ID"] == str(TENANT)
        assert request.headers["X-Request-Fingerprint"] == audience_query_fingerprint(query())
        assert "Idempotency-Key" not in request.headers
        assert json.loads(request.content) == query().model_dump(mode="json")
        return httpx.Response(200, json=snapshot_payload())
    client = http_client(handle)
    try:
        assert client._client._trust_env is False
        result = await client.estimate(query())
        assert result.eligible_users == 80 and len(calls) == 1
        assert not {"user_id", "users", "events", "recipient_ids"} & type(result).model_fields.keys()
    finally:
        await client.close()


@pytest.mark.parametrize("status,code,retryable", [
    (429, "rate_limited", True), (503, "unavailable", True),
    (401, "unauthorized", False), (403, "unauthorized", False),
    (307, "redirect", False), (400, "rejected", False),
])
@pytest.mark.asyncio
async def test_http_status_is_safe_and_never_retried_or_redirected(status, code, retryable):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://private.example/token"},
                              text="PRIVATE_BODY PRIVATE_TOKEN")
    client = http_client(handle)
    try:
        with pytest.raises(AudienceProviderError) as caught:
            await client.estimate(query())
        assert caught.value.code == code and caught.value.retryable is retryable
        assert caught.value.status_code == status and len(calls) == 1
        assert "PRIVATE" not in str(caught.value) and "private.example" not in str(caught.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_timeout_is_bounded_safe_and_not_retried():
    calls = []
    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("PRIVATE_BODY PRIVATE_TOKEN", request=request)
    client = http_client(handle)
    try:
        with pytest.raises(AudienceProviderError) as caught:
            await client.estimate(query())
        assert caught.value.code == "timeout" and caught.value.retryable
        assert len(calls) == 1 and "PRIVATE" not in str(caught.value)
    finally:
        await client.close()


@pytest.mark.parametrize("kind", ["bad_json", "duplicate", "nan", "extra_users", "wrong_tenant",
                                  "wrong_run", "wrong_query", "synthetic", "encoded"])
@pytest.mark.asyncio
async def test_http_rejects_untrusted_response_contract(kind):
    payload = snapshot_payload()
    headers = {"Content-Type": "application/json"}
    if kind == "bad_json":
        raw = b"PRIVATE_NOT_JSON"
    elif kind == "duplicate":
        raw = b'{"matched_users":1,"matched_users":2}'
    elif kind == "nan":
        raw = b'{"matched_users":NaN}'
    else:
        if kind == "extra_users": payload["users"] = ["PRIVATE_USER"]
        if kind == "wrong_tenant": payload["tenant_id"] = str(uuid4())
        if kind == "wrong_run": payload["run_id"] = str(uuid4())
        if kind == "wrong_query": payload["query_fingerprint"] = "a" * 64
        if kind == "synthetic": payload["source_mode"] = "synthetic"
        if kind == "encoded": headers["Content-Encoding"] = "gzip"
        raw = json.dumps(payload).encode()
    client = http_client(lambda _: httpx.Response(200, content=raw, headers=headers))
    try:
        with pytest.raises(AudienceProviderError) as caught:
            await client.estimate(query())
        assert caught.value.code == "invalid_response" and not caught.value.retryable
        assert "PRIVATE" not in str(caught.value)
    finally:
        await client.close()


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"x" * 20
        yield b"x" * 20


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.asyncio
async def test_http_response_limit_covers_declared_and_streamed_sizes(declared):
    def handle(_):
        headers = {"Content-Type": "application/json"}
        if declared:
            headers["Content-Length"] = "100000"
        return httpx.Response(200, headers=headers, stream=Chunks())
    client = http_client(handle, max_response_bytes=32)
    try:
        with pytest.raises(AudienceProviderError) as caught:
            await client.estimate(query())
        assert caught.value.code == "response_too_large"
    finally:
        await client.close()


@pytest.mark.parametrize("url", ["http://audience.example/v1/estimate", "file:///tmp/audience",
    "https://user:PRIVATE_TOKEN@example.invalid/v1/estimate", "https://example.invalid/?token=PRIVATE_TOKEN"])
def test_http_transport_requires_safe_https_configuration(url):
    with pytest.raises(ValueError) as caught:
        HttpAudienceProfileClient(endpoint_url=url, token="PRIVATE_TOKEN")
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.asyncio
async def test_synthetic_parent_contains_child_and_combination_uses_exact_union():
    client = LocalAudienceProfileClient(catalog(), environment="test", clock=lambda: NOW)
    parent = await client.estimate(query(include=("tech",)))
    child = await client.estimate(query(include=("tech.nature",)))
    both = await client.estimate(query(include=("tech", "tech.nature")))
    intersection = await client.estimate(query(include=("tech", "tech.nature"), match="all"))
    finance = await client.estimate(query(include=("finance",)))
    union = await client.estimate(query(include=("tech", "finance")))
    excluded = await client.estimate(query(include=("tech",), exclude=("tech.nature",)))
    assert parent.matched_users >= child.matched_users > 0
    assert both.matched_users == parent.matched_users
    assert intersection.matched_users == child.matched_users
    assert union.matched_users < parent.matched_users + finance.matched_users
    assert excluded.matched_users == parent.matched_users - child.matched_users
    assert 0 < parent.eligible_users < parent.matched_users
    assert parent.watermark == NOW and parent.is_complete
    assert parent.expires_at - parent.generated_at == timedelta(minutes=5)
    assert parent.source_mode == "synthetic"
    assert not {"user_id", "users", "events", "recipient_ids"} & parent.model_dump().keys()


@pytest.mark.parametrize("include", [("unknown",), ("tech.disabled",)])
@pytest.mark.asyncio
async def test_synthetic_rejects_unknown_or_disabled_topics(include):
    client = LocalAudienceProfileClient(catalog(), environment="test", clock=lambda: NOW)
    with pytest.raises(AudienceProviderError):
        await client.estimate(query(include=include))


def settings(**overrides):
    values = dict(audience_backend="synthetic", environment="test", audience_api_token=SecretStr("PRIVATE_TOKEN"),
        audience_estimate_url="https://audience.example.invalid/v1/estimate", audience_timeout_seconds=5,
        audience_max_response_bytes=262144)
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("environment", ["production", "prod", "staging"])
def test_factory_never_allows_synthetic_in_production_like_environments(environment):
    with pytest.raises(ValueError):
        build_audience_client(settings(environment=environment), catalog())


def test_factory_requires_enterprise_catalog_approval_and_https():
    with pytest.raises(ValueError, match="approved"):
        build_audience_client(settings(audience_backend="enterprise_http"), catalog())
    with pytest.raises(ValueError, match="URL"):
        build_audience_client(settings(audience_backend="enterprise_http",
            audience_estimate_url="http://localhost/v1/estimate"), catalog(status="approved"))


@pytest.mark.asyncio
async def test_factory_builds_explicit_backend_and_closes_it():
    client = build_audience_client(settings(), catalog())
    assert isinstance(client, LocalAudienceProfileClient)
    await client.close()
    with pytest.raises(AudienceProviderError) as caught:
        await client.estimate(query())
    assert caught.value.code == "closed"
