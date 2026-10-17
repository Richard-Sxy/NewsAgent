"""Reader aggregate transport, causal validation and bounded sharing contracts."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
from uuid import UUID

import httpx
from pydantic import SecretStr, ValidationError
import pytest

from app.audience.taxonomy import Topic, TopicCatalog
from app.reader_profiles.client import HttpNewsReaderProfileClient
from app.reader_profiles.contracts import (
    IDENTITY_POLICY_VERSION, PROFILE_POLICY_VERSION, ReaderProfileBatch, ReaderProfileProviderError,
    ReaderProfileQuery, ReaderProfileSummary, reader_profile_query_fingerprint,
)
from app.reader_profiles.service import ReaderProfileService


NOW = datetime(2026, 10, 17, 8, tzinfo=UTC)
TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = UUID("22222222-2222-4222-8222-222222222222")
RELEASE = "b" * 64
TAXONOMY = "reader-test-topics-v1"


def catalog(status="approved"):
    return TopicCatalog(taxonomy_version=TAXONOMY, status=status, topics=(
        Topic(topic_id="tech", name="科技", provenance="user_confirmed"),
        Topic(topic_id="tech.robotics", name="机器人", parent_topic_id="tech", provenance="user_confirmed"),
    ))


def query(**overrides):
    fields = dict(tenant_id=TENANT, news_ids=("news-1",), window_start=NOW-timedelta(hours=1),
                  window_end=NOW, source_release_sha256=RELEASE)
    fields.update(overrides)
    return ReaderProfileQuery(**fields)


def batch_payload(request=None, *, mode="enterprise", now=NOW, expires_seconds=300):
    request = request or query()
    interest = dict(lookback_hours=720, profiled_reader_count=8, unknown_reader_count=2,
        cold_start_reader_count=1, baseline_profiled_count=100,
        topics=[dict(topic_id="tech.robotics", reader_count=5, share_all="0.50000000",
            share_profiled="0.62500000", baseline_reader_count=20, baseline_profiled_count=100,
            baseline_share="0.20000000", lift="3.12500000", low_sample=False)])
    items = [dict(news_id=news_id, source_mode=mode, taxonomy_version=TAXONOMY,
        profile_policy_version=PROFILE_POLICY_VERSION, content_sha256="c"*64,
        content_topic_ids=["tech", "tech.robotics"], reader_count=10,
        long_term=interest, short_term={**interest, "lookback_hours": 24},
        profile_cutoff=request.window_start, trigger_article_excluded=True,
        identity_policy_version="enterprise-user-id-tenant-scoped-v1" if mode == "enterprise" else IDENTITY_POLICY_VERSION,
        count_method="enterprise_exact_distinct" if mode == "enterprise" else "exact_synthetic_distinct",
        status="complete") for news_id in request.news_ids]
    values = dict(tenant_id=request.tenant_id, window_start=request.window_start,
        window_end=request.window_end, source_release_sha256=RELEASE,
        query_fingerprint=reader_profile_query_fingerprint(request), snapshot_id="reader-snapshot-1",
        source_revision=1, taxonomy_version=TAXONOMY, profile_policy_version=PROFILE_POLICY_VERSION,
        generated_at=now, expires_at=now+timedelta(seconds=expires_seconds) if expires_seconds else None,
        watermark=request.window_end, is_complete=True, source_mode=mode, items=items)
    return ReaderProfileBatch(**values).model_dump(mode="json")


def http_client(handler, **overrides):
    options = dict(endpoint_url="https://reader.example.invalid/v1/batch", token=SecretStr("PRIVATE_TOKEN"),
        expected_source_release_sha256=RELEASE, expected_taxonomy_version=TAXONOMY,
        expected_profile_policy_version=PROFILE_POLICY_VERSION, environment="test",
        transport=httpx.MockTransport(handler), clock=lambda: NOW)
    options.update(overrides)
    return HttpNewsReaderProfileClient(**options)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Port:
    identity_sha256 = "a"*64
    source_release_sha256 = RELEASE
    expected_source_mode = "enterprise"
    expected_taxonomy_version = TAXONOMY
    expected_profile_policy_version = PROFILE_POLICY_VERSION
    environment = "test"

    def __init__(self, *, clock=lambda: NOW, gate=None, mode="enterprise", expires_seconds=300):
        self.clock, self.gate = clock, gate
        self.expected_source_mode = mode
        self.expires_seconds = expires_seconds
        self.calls = []
        self.started = asyncio.Event()
        self.cancelled = False
        self.close_calls = 0
        self.mutate = None
        self.fail = None

    async def fetch(self, request):
        self.calls.append(request)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.fail is not None:
                raise self.fail
            batch = ReaderProfileBatch.model_validate(batch_payload(request, mode=self.expected_source_mode,
                now=self.clock(), expires_seconds=self.expires_seconds))
            return self.mutate(batch) if self.mutate else batch
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def close(self):
        self.close_calls += 1


def service(port, **overrides):
    options = dict(catalog=catalog(), clock=port.clock)
    options.update(overrides)
    return ReaderProfileService(port, **options)


@pytest.mark.parametrize("missing", [True, False])
def test_enterprise_summary_rejects_missing_or_synthetic_identity_policy(missing):
    item = batch_payload()["items"][0]
    if missing:
        item.pop("identity_policy_version")
    else:
        item["identity_policy_version"] = IDENTITY_POLICY_VERSION
    with pytest.raises(ValidationError, match="explicit enterprise identity policy"):
        ReaderProfileSummary.model_validate(item)


def test_enterprise_summary_accepts_explicit_enterprise_identity_policy():
    item = ReaderProfileSummary.model_validate(batch_payload()["items"][0])
    assert item.identity_policy_version == "enterprise-user-id-tenant-scoped-v1"


def test_synthetic_summary_retains_synthetic_identity_policy_default():
    item = batch_payload(mode="synthetic")["items"][0]
    item.pop("identity_policy_version")
    assert ReaderProfileSummary.model_validate(item).identity_policy_version == IDENTITY_POLICY_VERSION


@pytest.mark.asyncio
async def test_https_adapter_one_post_with_private_token_only_in_authorization():
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.headers["X-Tenant-ID"] == str(TENANT)
        assert request.headers["Authorization"] == "Bearer PRIVATE_TOKEN"
        assert request.headers["X-Request-Fingerprint"] == reader_profile_query_fingerprint(query())
        assert "Idempotency-Key" not in request.headers
        assert json.loads(request.content) == query().model_dump(mode="json")
        return httpx.Response(200, json=batch_payload())
    client = http_client(handle)
    try:
        assert not client._client._trust_env and not client._client.follow_redirects
        batch = await client.fetch(query())
        assert batch.items[0].reader_count == 10 and len(calls) == 1
        assert len(client.identity_sha256) == 64
        assert not {"user_id", "users", "events", "recipient_ids"} & type(batch.items[0]).model_fields.keys()
        assert "PRIVATE_TOKEN" not in client.identity_sha256
    finally:
        await client.close()


@pytest.mark.parametrize("status,code,retryable", [
    (302, "redirect", False), (307, "redirect", False), (401, "unauthorized", False),
    (403, "unauthorized", False), (429, "rate_limited", True), (503, "unavailable", True),
])
@pytest.mark.asyncio
async def test_http_status_errors_are_safe_and_never_retry_or_redirect(status, code, retryable):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://PRIVATE.invalid/token"}, text="PRIVATE_BODY")
    client = http_client(handle)
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await client.fetch(query())
        assert (caught.value.code, caught.value.retryable, caught.value.status_code) == (code, retryable, status)
        assert "PRIVATE" not in str(caught.value) and len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_transport_timeout_is_safe_and_not_retried():
    calls = []
    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("PRIVATE_TOKEN PRIVATE_BODY", request=request)
    client = http_client(handle)
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await client.fetch(query())
        assert caught.value.code == "timeout" and caught.value.retryable
        assert "PRIVATE" not in str(caught.value) and len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_adapter_sanitizes_unexpected_transport_errors():
    def handle(_request):
        raise ValueError("PRIVATE_TOKEN PRIVATE_BODY")
    client = http_client(handle)
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await client.fetch(query())
        assert caught.value.code == "unavailable" and "PRIVATE" not in str(caught.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_total_deadline_includes_slow_response():
    calls = []
    async def handle(request):
        calls.append(request)
        await asyncio.Event().wait()
    client = http_client(handle, timeout_seconds=0.02)
    try:
        with pytest.raises(ReaderProfileProviderError, match="timed out"):
            await client.fetch(query())
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("payload", [
    b'{"tenant_id":"private", "tenant_id":"private"}', b'{"value":NaN}',
    b'{"value":Infinity}', b'not JSON PRIVATE_BODY', b'\xff',
])
@pytest.mark.asyncio
async def test_strict_json_rejects_duplicates_nonfinite_and_bad_encoding(payload):
    client = http_client(lambda _: httpx.Response(200, content=payload,
        headers={"Content-Type": "application/json"}))
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await client.fetch(query())
        assert caught.value.code == "invalid_response" and "private" not in str(caught.value)
    finally:
        await client.close()


@pytest.mark.parametrize("location", ["batch", "summary", "interest", "topic"])
@pytest.mark.asyncio
async def test_schema_rejects_personal_fields_at_every_response_level(location):
    payload = batch_payload()
    target = {"batch": payload, "summary": payload["items"][0],
        "interest": payload["items"][0]["long_term"],
        "topic": payload["items"][0]["long_term"]["topics"][0]}[location]
    target["user_id"] = "PRIVATE_USER"
    client = http_client(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await client.fetch(query())
        assert caught.value.code == "invalid_response" and "PRIVATE" not in str(caught.value)
    finally:
        await client.close()


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"a"*4000
        yield b"b"*4000


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.asyncio
async def test_http_response_cap_applies_to_headers_and_actual_stream(declared):
    headers = {"Content-Type": "application/json"}
    if declared:
        headers["Content-Length"] = "8000"
    client = http_client(lambda _: httpx.Response(200, headers=headers, stream=Chunks()), max_response_bytes=4096)
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await client.fetch(query())
        assert caught.value.code == "response_too_large"
    finally:
        await client.close()


@pytest.mark.parametrize("headers", [
    {"Content-Type": "text/html"}, {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    {"Content-Type": "application/json", "Content-Length": "-1"},
])
@pytest.mark.asyncio
async def test_http_rejects_unbounded_or_non_json_encodings(headers):
    client = http_client(lambda _: httpx.Response(200, headers=headers, stream=Chunks()))
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await client.fetch(query())
        assert caught.value.code == "invalid_response"
    finally:
        await client.close()


@pytest.mark.parametrize("options", [
    {"endpoint_url": "http://localhost/batch"}, {"endpoint_url": "https://name:secret@host/batch"},
    {"endpoint_url": "https://host/batch?token=secret"}, {"endpoint_url": "https://host/batch#secret"},
    {"endpoint_url": "https://host/\nsecret"}, {"token": "PRIVATE_TOKEN"},
    {"token": SecretStr("private\r\nvalue")}, {"expected_source_release_sha256": "not-a-digest"},
    {"timeout_seconds": True}, {"timeout_seconds": float("nan")},
    {"max_response_bytes": True}, {"environment": "unconfigured"},
])
def test_transport_configuration_is_strict(options):
    with pytest.raises(ValueError):
        http_client(lambda _: httpx.Response(200), **options)


@pytest.mark.asyncio
async def test_direct_adapter_checks_binding_and_missing_news_does_not_become_zero():
    payload = batch_payload()
    payload["items"][0]["news_id"] = "unrequested-news"
    client = http_client(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await client.fetch(query())
        assert caught.value.code == "invalid_response"
        with pytest.raises(ReaderProfileProviderError) as invalid:
            await client.fetch(query(source_release_sha256="d"*64))
        assert invalid.value.code == "invalid_query"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_singleflight_cache_and_waiter_cancellation_do_not_cancel_other_callers():
    gate = asyncio.Event()
    port = Port(gate=gate)
    index = service(port)
    first = asyncio.create_task(index.fetch(query()))
    await port.started.wait()
    second = asyncio.create_task(index.fetch(query()))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    try:
        result = await second
        assert result.items[0].reader_count == 10 and not port.cancelled and len(port.calls) == 1
        assert (await index.fetch(query())).snapshot_id == result.snapshot_id
        assert len(port.calls) == 1
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_cache_key_binds_tenant_window_full_ids_and_canonical_order():
    port = Port()
    index = service(port)
    requests = [query(news_ids=("news-2", "news-1")), query(news_ids=("news-1", "news-2")),
        query(news_ids=("news-1",)), query(tenant_id=OTHER_TENANT),
        query(window_start=NOW-timedelta(hours=2))]
    try:
        for request in requests:
            await index.fetch(request)
        assert len(port.calls) == 4
        with pytest.raises(ReaderProfileProviderError) as caught:
            await index.fetch(query(source_release_sha256="d"*64))
        assert caught.value.code == "invalid_query" and len(port.calls) == 4
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_service_rejects_future_window_before_provider_call():
    port = Port()
    index = service(port)
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await index.fetch(query(window_end=NOW+timedelta(seconds=1)))
        assert caught.value.code == "invalid_query" and not port.calls
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_ttl_and_enterprise_expiry_bound_cache_lifetime():
    clock = Clock()
    port = Port(clock=clock, expires_seconds=5)
    index = service(port, cache_ttl_seconds=30)
    try:
        await index.fetch(query())
        clock.now += timedelta(seconds=4)
        await index.fetch(query())
        assert len(port.calls) == 1
        clock.now += timedelta(seconds=2)
        await index.fetch(query())
        assert len(port.calls) == 2
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_synthetic_historical_snapshot_without_expiry_still_has_bounded_ttl():
    clock = Clock()
    port = Port(clock=clock, mode="synthetic", expires_seconds=0)
    index = service(port, cache_ttl_seconds=10, catalog=catalog("draft"))
    try:
        await index.fetch(query())
        clock.now += timedelta(seconds=11)
        await index.fetch(query())
        assert len(port.calls) == 2
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_lru_cache_capacity_is_bounded():
    port = Port()
    index = service(port, max_cache_entries=2)
    try:
        for name in ["n1", "n2", "n3", "n1"]:
            await index.fetch(query(news_ids=(name,)))
        assert len(port.calls) == 4 and len(index._cache) == 2
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_zero_ttl_disables_cache_but_retains_singleflight():
    gate = asyncio.Event()
    port = Port(gate=gate)
    index = service(port, cache_ttl_seconds=0)
    first = asyncio.create_task(index.fetch(query()))
    await port.started.wait()
    second = asyncio.create_task(index.fetch(query()))
    await asyncio.sleep(0)
    gate.set()
    try:
        await asyncio.gather(first, second)
        assert len(port.calls) == 1 and not index._cache
        await index.fetch(query())
        assert len(port.calls) == 2 and not index._cache
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_unique_inflight_capacity_is_bounded_but_existing_waiters_can_join():
    gate = asyncio.Event()
    port = Port(gate=gate)
    index = service(port, max_inflight=1)
    first = asyncio.create_task(index.fetch(query()))
    await port.started.wait()
    joined = asyncio.create_task(index.fetch(query()))
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await index.fetch(query(news_ids=("news-other",)))
        assert caught.value.code == "busy" and caught.value.retryable
        gate.set()
        await asyncio.gather(first, joined)
        assert len(port.calls) == 1
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_service_deadline_stops_shared_producer_and_never_caches_exception():
    port = Port(gate=asyncio.Event())
    index = service(port, timeout_seconds=0.02)
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await index.fetch(query())
        assert caught.value.code == "timeout"
        await asyncio.sleep(0)
        assert port.cancelled and not index._cache and not index._inflight
        port.gate = None
        await index.fetch(query())
        assert len(port.calls) == 2
    finally:
        await index.close()


@pytest.mark.parametrize("error", [ReaderProfileProviderError("rate_limited", retryable=True),
    ValueError("PRIVATE_BODY PRIVATE_USER"), OSError("PRIVATE_BODY PRIVATE_USER")])
@pytest.mark.asyncio
async def test_provider_errors_are_safe_not_cached_and_can_be_retried_by_a_later_request(error):
    port = Port()
    port.fail = error
    index = service(port)
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await index.fetch(query())
        assert "PRIVATE" not in str(caught.value) and not index._cache
        port.fail = None
        await index.fetch(query())
        assert len(port.calls) == 2
    finally:
        await index.close()


@pytest.mark.parametrize("field,value", [
    ("tenant_id", OTHER_TENANT), ("source_release_sha256", "d"*64), ("query_fingerprint", "0"*64),
    ("window_start", NOW-timedelta(hours=2)), ("is_complete", False),
    ("watermark", NOW-timedelta(seconds=1)), ("watermark", NOW+timedelta(seconds=1)),
    ("generated_at", NOW+timedelta(seconds=1)), ("taxonomy_version", "wrong-v1"),
    ("profile_policy_version", "wrong-v1"), ("source_mode", "synthetic"),
])
@pytest.mark.asyncio
async def test_service_rejects_forged_batch_bindings_without_caching(field, value):
    port = Port()
    port.mutate = lambda batch: batch.model_copy(update={field: value})
    index = service(port)
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await index.fetch(query())
        assert caught.value.code == "invalid_response" and not index._cache
    finally:
        await index.close()


@pytest.mark.parametrize("changes", [
    {"news_id": "other-news"}, {"profile_cutoff": NOW}, {"trigger_article_excluded": False},
    {"reader_definition": "impressions-are-readers"}, {"content_topic_ids": ("unknown.topic",)},
])
@pytest.mark.asyncio
async def test_service_rejects_future_self_influenced_or_unknown_summary(changes):
    port = Port()
    port.mutate = lambda batch: batch.model_copy(update={"items": (batch.items[0].model_copy(update=changes),)})
    index = service(port)
    try:
        with pytest.raises(ReaderProfileProviderError) as caught:
            await index.fetch(query())
        assert caught.value.code == "invalid_response"
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_service_checks_interest_topic_catalog_and_exact_requested_set():
    port = Port()
    def change(batch):
        item = batch.items[0]
        interest = item.long_term
        topic = interest.topics[0].model_copy(update={"topic_id": "unknown.topic"})
        return batch.model_copy(update={"items": (item.model_copy(update={
            "long_term": interest.model_copy(update={"topics": (topic,)})}),)})
    port.mutate = change
    index = service(port)
    try:
        with pytest.raises(ReaderProfileProviderError):
            await index.fetch(query())
        port.mutate = lambda batch: batch.model_copy(update={"items": (batch.items[0],)})
        with pytest.raises(ReaderProfileProviderError):
            await index.fetch(query(news_ids=("news-1", "news-2")))
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_zero_readers_are_valid_only_when_explicitly_complete_empty_and_cover_query():
    port = Port()
    def empty(batch):
        payload = batch.model_dump(mode="json")
        item = payload["items"][0]
        item.update(reader_count=0, status="empty")
        for name in ("long_term", "short_term"):
            item[name].update(profiled_reader_count=0, unknown_reader_count=0,
                              cold_start_reader_count=0, topics=[])
        return ReaderProfileBatch.model_validate(payload)
    port.mutate = empty
    index = service(port)
    try:
        result = await index.fetch(query())
        assert result.items[0].reader_count == 0 and result.items[0].status == "empty"
    finally:
        await index.close()


def test_service_rejects_synthetic_production_and_unapproved_enterprise_catalog():
    port = Port(mode="synthetic")
    with pytest.raises(ValueError):
        service(port, environment="production")
    with pytest.raises(ValueError):
        service(Port(), catalog=catalog("draft"))


def test_context_identity_binds_tenant_window_release_policy_and_canonical_timezone():
    port = Port()
    index = service(port)
    first = index.context_identity_for_window(tenant_id=TENANT, window_start=query().window_start, window_end=NOW)
    assert len(first) == 64
    assert first == index.context_identity_for_window(tenant_id=TENANT,
        window_start=query().window_start.astimezone(UTC), window_end=NOW)
    assert first != index.context_identity_for_window(tenant_id=OTHER_TENANT,
        window_start=query().window_start, window_end=NOW)
    assert first != index.context_identity_for_window(tenant_id=TENANT,
        window_start=NOW-timedelta(hours=2), window_end=NOW)
    port2 = Port()
    port2.source_release_sha256 = "e"*64
    assert index.identity_sha256 != service(port2).identity_sha256
    port3 = Port()
    port3.identity_sha256 = "f"*64
    assert index.identity_sha256 != service(port3).identity_sha256


@pytest.mark.asyncio
async def test_close_cancels_background_tasks_clears_cache_and_closes_port_once():
    port = Port(gate=asyncio.Event())
    index = service(port)
    waiting = asyncio.create_task(index.fetch(query()))
    await port.started.wait()
    await index.close()
    assert not index._cache and not index._inflight and port.cancelled and port.close_calls == 1
    assert isinstance((await asyncio.gather(waiting, return_exceptions=True))[0], asyncio.CancelledError)
    await index.close()
    assert port.close_calls == 1
    with pytest.raises(ReaderProfileProviderError) as caught:
        await index.fetch(query())
    assert caught.value.code == "closed"
