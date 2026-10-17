"""Tenant API boundaries and explicit, resource-safe aggregate-provider assembly."""

from datetime import timedelta
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest

from app.api.dependencies import get_data_loop_gateway_token
from app.api.reader_profiles import router
from app.modules.reader_profiles.contracts import PROFILE_POLICY_VERSION, ReaderProfileBatch, ReaderProfileProviderError
from app.modules.reader_profiles.factory import build_reader_profile_service
from tests.test_reader_profile_client_service import (
    NOW, OTHER_TENANT, RELEASE, TENANT, batch_payload, catalog,
)


PATH = "/api/v1/reader-profiles/query"
USER = "33333333-3333-4333-8333-333333333333"


class ApiService:
    source_release_sha256 = RELEASE

    def __init__(self):
        self.calls = []
        self.error = None

    async def fetch(self, query):
        self.calls.append(query)
        if self.error:
            raise self.error
        return ReaderProfileBatch.model_validate(batch_payload(query))


def app_for(index=None, *, token="TEST_GATEWAY"):
    app = FastAPI()
    app.include_router(router)
    app.state.reader_profile_service = index
    app.dependency_overrides[get_data_loop_gateway_token] = lambda: token
    return app


def headers(*, tenant=TENANT, roles="hot-news:read"):
    return {"Authorization": "Bearer TEST_GATEWAY", "X-Tenant-ID": str(tenant),
        "X-User-ID": USER, "X-Hot-News-Roles": roles}


def body(**changes):
    value = dict(news_ids=["news-1"], window_start=(NOW-timedelta(hours=1)).isoformat(),
                 window_end=NOW.isoformat())
    value.update(changes)
    return value


def settings(**changes):
    value = dict(reader_profile_enabled=True, reader_profile_backend="enterprise_http", environment="test",
        reader_profile_package_path=None, reader_profile_taxonomy_path=None,
        reader_profile_source_release_sha256=RELEASE,
        reader_profile_policy_version=PROFILE_POLICY_VERSION,
        reader_profile_allowed_analysis_prompt_versions=("native-e2e-prompt-v3-reader-profiles",),
        reader_profile_timeout_seconds=3, reader_profile_run_timeout_seconds=5,
        reader_profile_cache_ttl_seconds=30, reader_profile_max_cache_entries=256,
        reader_profile_max_inflight=16, reader_profile_max_response_bytes=524288,
        reader_profile_endpoint_url="https://reader.example.invalid/v1/batch",
        reader_profile_api_token=SecretStr("TEST_READER_TOKEN"))
    value.update(changes)
    return SimpleNamespace(**value)


@pytest.fixture
def taxonomy_path(tmp_path):
    target = tmp_path / "topics.json"
    target.write_text(json.dumps(catalog().model_dump(mode="json")), encoding="utf-8")
    return str(target)


@pytest.fixture
def package(tmp_path):
    from app.modules.reader_profiles.build import build_package
    from tests.test_reader_profile_build import write_source
    source = write_source(tmp_path / "source")
    target = tmp_path / "reader-package"
    build_package(source, target)
    return target


def test_api_uses_only_trusted_gateway_tenant_and_bound_service_release():
    index = ApiService()
    with TestClient(app_for(index)) as client:
        first = client.post(PATH, headers=headers(), json=body(news_ids=["news-2", "news-1"]))
        second = client.post(PATH, headers=headers(tenant=OTHER_TENANT), json=body())
    assert first.status_code == second.status_code == 200
    assert index.calls[0].tenant_id == TENANT and index.calls[1].tenant_id == OTHER_TENANT
    assert index.calls[0].news_ids == ("news-1", "news-2")
    assert all(query.source_release_sha256 == RELEASE for query in index.calls)
    assert first.json()["snapshot"]["tenant_id"] == str(TENANT)
    assert second.json()["snapshot"]["tenant_id"] == str(OTHER_TENANT)
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json()["dispatch_enabled"] is False and first.json()["manual_review_required"] is True
    assert not any(field in first.text for field in ("user_id", "synthetic_user_id", "recipient_ids", "events", USER))


@pytest.mark.parametrize("extra", [
    {"tenant_id": str(OTHER_TENANT)}, {"source_release_sha256": "f"*64},
    {"user_id": USER}, {"news_reader_ids": [USER]},
])
def test_api_body_cannot_override_identity_release_or_request_individuals(extra):
    index = ApiService()
    with TestClient(app_for(index)) as client:
        result = client.post(PATH, headers=headers(), json={**body(), **extra})
    assert result.status_code == 422 and not index.calls


def test_disabled_api_returns_503():
    with TestClient(app_for()) as client:
        result = client.post(PATH, headers=headers(), json=body())
    assert result.status_code == 503


@pytest.mark.parametrize("kind,status", [
    ("no_token", 401), ("wrong_token", 401), ("no_identity", 401),
    ("no_read_permission", 403), ("unconfigured_gateway", 503),
])
def test_api_requires_gateway_authentication_and_hot_news_read_permission(kind, status):
    index = ApiService()
    request_headers = headers()
    if kind == "no_token":
        request_headers.pop("Authorization")
    elif kind == "wrong_token":
        request_headers["Authorization"] = "Bearer WRONG"
    elif kind == "no_identity":
        request_headers.pop("X-User-ID")
    elif kind == "no_read_permission":
        request_headers["X-Hot-News-Roles"] = "hot-news:decide"
    with TestClient(app_for(index, token=None if kind == "unconfigured_gateway" else "TEST_GATEWAY")) as client:
        result = client.post(PATH, headers=request_headers, json=body())
    assert result.status_code == status and not index.calls


@pytest.mark.parametrize("change", [
    {"news_ids": []}, {"news_ids": ["news-1"]*2}, {"news_ids": [f"n{i}" for i in range(21)]},
    {"news_ids": [True]}, {"window_start": NOW.replace(tzinfo=None).isoformat()},
    {"window_start": NOW.isoformat()}, {"window_start": (NOW-timedelta(hours=25)).isoformat()},
])
def test_api_rejects_invalid_or_unbounded_batch_without_provider_call(change):
    index = ApiService()
    with TestClient(app_for(index)) as client:
        result = client.post(PATH, headers=headers(), json=body(**change))
    assert result.status_code == 422 and not index.calls


def test_api_accepts_exact_maximum_of_twenty_news_ids():
    index = ApiService()
    with TestClient(app_for(index)) as client:
        result = client.post(PATH, headers=headers(), json=body(news_ids=[f"news-{i}" for i in range(20)]))
    assert result.status_code == 200 and len(result.json()["snapshot"]["items"]) == 20
    assert len(index.calls) == 1 and len(index.calls[0].news_ids) == 20


@pytest.mark.parametrize("error,status", [
    (ReaderProfileProviderError("invalid_query"), 422),
    (ReaderProfileProviderError("timeout", retryable=True), 503),
    (ReaderProfileProviderError("invalid_response"), 503),
    (ReaderProfileProviderError("rate_limited", retryable=True), 503),
    (ValueError("PRIVATE_TENANT PRIVATE_RESPONSE"), 422),
])
def test_api_maps_provider_failures_without_inventing_zero_or_leaking_data(error, status):
    index = ApiService()
    index.error = error
    with TestClient(app_for(index)) as client:
        result = client.post(PATH, headers=headers(), json=body())
    assert result.status_code == status
    assert "snapshot" not in result.json() and "reader_count" not in result.text and "PRIVATE" not in result.text


def test_disabled_factory_reads_no_package_or_other_configuration():
    class Disabled:
        reader_profile_enabled = False
        def __getattr__(self, _name):
            raise AssertionError("disabled factory inspected another option")
    assert build_reader_profile_service(Disabled()) is None


@pytest.mark.parametrize("change", [
    {"reader_profile_allowed_analysis_prompt_versions": ()},
    {"reader_profile_allowed_analysis_prompt_versions": ("v1", "v1")},
    {"reader_profile_allowed_analysis_prompt_versions": (" ",)},
    {"reader_profile_allowed_analysis_prompt_versions": ("v1\n",)},
    {"reader_profile_allowed_analysis_prompt_versions": "v1"},
    {"reader_profile_timeout_seconds": True}, {"reader_profile_timeout_seconds": 0},
    {"reader_profile_run_timeout_seconds": float("nan")},
    {"reader_profile_cache_ttl_seconds": -1}, {"reader_profile_max_inflight": 0},
    {"reader_profile_max_cache_entries": 0}, {"reader_profile_max_response_bytes": 3},
    {"reader_profile_policy_version": ""}, {"reader_profile_source_release_sha256": "bad-digest"},
    {"environment": "unconfigured"},
])
def test_factory_checks_all_common_options_before_creating_http_client(taxonomy_path, monkeypatch, change):
    import app.modules.reader_profiles.client as client_module
    called = []
    def constructor(**_options):
        called.append(True)
        raise AssertionError("HTTP client must not be constructed for invalid options")
    monkeypatch.setattr(client_module, "HttpNewsReaderProfileClient", constructor)
    with pytest.raises(ValueError):
        build_reader_profile_service(settings(reader_profile_taxonomy_path=taxonomy_path, **change))
    assert not called


@pytest.mark.parametrize("change", [
    {"reader_profile_api_token": None}, {"reader_profile_api_token": "plain-token"},
    {"reader_profile_taxonomy_path": None}, {"reader_profile_source_release_sha256": None},
])
def test_enterprise_factory_requires_token_taxonomy_and_release_before_client(taxonomy_path, monkeypatch, change):
    import app.modules.reader_profiles.client as client_module
    calls = []
    monkeypatch.setattr(client_module, "HttpNewsReaderProfileClient", lambda **_kwargs: calls.append(True))
    options = dict(reader_profile_taxonomy_path=taxonomy_path)
    options.update(change)
    with pytest.raises(ValueError):
        build_reader_profile_service(settings(**options))
    assert not calls


def test_enterprise_factory_rejects_draft_taxonomy_before_creating_client(tmp_path, monkeypatch):
    import app.modules.reader_profiles.client as client_module
    target = tmp_path / "draft.json"
    target.write_text(json.dumps(catalog("draft").model_dump(mode="json")), encoding="utf-8")
    calls = []
    monkeypatch.setattr(client_module, "HttpNewsReaderProfileClient", lambda **_kwargs: calls.append(True))
    with pytest.raises(ValueError, match="approved"):
        build_reader_profile_service(settings(reader_profile_taxonomy_path=str(target)))
    assert not calls


@pytest.mark.asyncio
async def test_enterprise_factory_returns_explicit_bound_service_and_zero_ttl_is_valid(taxonomy_path):
    index = build_reader_profile_service(settings(reader_profile_taxonomy_path=taxonomy_path,
        reader_profile_cache_ttl_seconds=0))
    try:
        assert index.environment == "test" and index.client.expected_source_mode == "enterprise"
        assert index.source_release_sha256 == RELEASE and index.catalog.status == "approved"
        assert index.allowed_analysis_prompt_versions == frozenset({"native-e2e-prompt-v3-reader-profiles"})
        assert index.run_timeout_seconds == 5 and index.cache_ttl_seconds == 0
        assert index.client.max_response_bytes == 524288 and index.client._client._trust_env is False
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_local_factory_loads_only_explicit_package_and_binds_release_policy_catalog(package):
    index = build_reader_profile_service(settings(reader_profile_backend="local_package",
        reader_profile_package_path=str(package), reader_profile_source_release_sha256=None))
    try:
        assert index.client.expected_source_mode == "synthetic" and index.environment == "test"
        assert index.catalog == index.client.catalog
        assert len(index.source_release_sha256) == 64
        from tests.test_reader_profile_local import query as local_query
        result = await index.fetch(local_query(index.client))
        assert result.items[0].reader_count == 3 and result.source_mode == "synthetic"
    finally:
        await index.close()


@pytest.mark.parametrize("change", [
    {"reader_profile_package_path": None}, {"reader_profile_source_release_sha256": "f"*64},
    {"reader_profile_policy_version": "different-policy-v2"},
])
def test_local_factory_rejects_missing_package_release_or_policy_mismatch(package, change):
    options = dict(reader_profile_backend="local_package", reader_profile_package_path=str(package),
        reader_profile_source_release_sha256=None)
    options.update(change)
    with pytest.raises(ValueError):
        build_reader_profile_service(settings(**options))


def test_local_factory_rejects_production_before_reading_package(monkeypatch):
    import app.modules.reader_profiles.local as local_module
    calls = []
    monkeypatch.setattr(local_module, "LocalNewsReaderProfileClient", lambda *_args, **_kwargs: calls.append(True))
    with pytest.raises(ValueError):
        build_reader_profile_service(settings(reader_profile_backend="local_package",
            reader_profile_package_path="/does/not/exist", environment="production"))
    assert not calls

