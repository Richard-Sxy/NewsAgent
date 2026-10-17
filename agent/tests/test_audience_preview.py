"""The vertical slice uses existing hot-news identity and never sends a push."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.api.audience import get_audience_service
from app.api.dependencies import get_data_loop_gateway_token
from app.audience.contracts import (
    AudienceProviderError, AudienceSelection, AudienceSnapshot, audience_query_fingerprint,
)
from app.audience.local import LocalAudienceProfileClient
from app.audience.service import (
    AudiencePreviewService, AudienceSelectionError, AudienceSnapshotError, AudienceTargetNotFound,
)
from app.audience.taxonomy import load_topic_catalog
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)
TENANT, RUN, USER = uuid4(), uuid4(), uuid4()
NEWS = "news-audience-demo-1"
CATALOG = load_topic_catalog(ROOT / "deploy/audience-topics.local.yml")


def selection(**updates):
    return AudienceSelection.model_validate({"taxonomy_version": CATALOG.taxonomy_version,
        "include_topic_ids": ["tech.nature"], **updates})


def memory():
    return SimpleNamespace(tenant_id=str(TENANT), run_id=RUN, news_id=NEWS,
        analysis_input=SimpleNamespace(news_id=NEWS,
            window_start=NOW - timedelta(hours=2), window_end=NOW - timedelta(hours=1)),
        analysis_report=SimpleNamespace(news_id=NEWS))


def snapshot(query):
    return AudienceSnapshot(tenant_id=query.tenant_id, run_id=query.run_id,
        news_id=query.news_id, taxonomy_version=query.selection.taxonomy_version,
        query_fingerprint=audience_query_fingerprint(query), snapshot_id="snapshot-test-1",
        profile_version="profile-v1", source_version="warehouse-v1",
        window_start=query.window_start, window_end=query.window_end,
        generated_at=NOW, expires_at=NOW + timedelta(minutes=5), watermark=query.window_end,
        is_complete=True, matched_users=37, eligible_users=23, source_mode="enterprise")


def service(*, target=None, provider=None, mode="enterprise"):
    reader = SimpleNamespace(get_analysis_memory=AsyncMock(return_value=target or memory()))
    provider = provider or SimpleNamespace(estimate=AsyncMock(side_effect=snapshot), close=AsyncMock())
    result = AudiencePreviewService(catalog=CATALOG, client=provider, run_store=reader,
        source_mode=mode, clock=lambda: NOW)
    return result, reader, provider


async def preview(svc, value=None):
    return await svc.preview(tenant_id=TENANT, run_id=RUN, news_id=NEWS,
                             selection=value or selection())


@pytest.mark.asyncio
async def test_binds_authorized_observation_before_external_call():
    svc, reader, provider = service()
    result = await preview(svc)
    reader.get_analysis_memory.assert_awaited_once_with(tenant_id=str(TENANT), run_id=RUN, news_id=NEWS)
    query = provider.estimate.await_args.args[0]
    assert query.window_end == memory().analysis_input.window_end
    assert result.snapshot.eligible_users == 23
    assert result.preview_only and result.manual_review_required and not result.dispatch_enabled
    assert result.recommendation.action == "consider"
    assert len(result.fingerprint) == 64


@pytest.mark.asyncio
async def test_synthetic_port_complete_vertical_slice_is_explicit_and_deduplicated():
    provider = LocalAudienceProfileClient(CATALOG, environment="test", clock=lambda: NOW)
    svc, _, _ = service(provider=provider, mode="synthetic")
    result = await preview(svc, selection(include_topic_ids=["tech", "tech.nature"]))
    parent = await preview(svc, selection(include_topic_ids=["tech"]))
    assert result.snapshot.matched_users == parent.snapshot.matched_users
    assert result.snapshot.source_mode == "synthetic"
    assert any("合成" in line for line in result.limitations)
    assert "user_id" not in result.model_dump_json()
    assert "device_id" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_missing_target_does_not_call_provider():
    svc, reader, provider = service()
    reader.get_analysis_memory.return_value = None
    with pytest.raises(AudienceTargetNotFound):
        await preview(svc)
    provider.estimate.assert_not_awaited()


@pytest.mark.asyncio
async def test_future_observation_window_is_blocked_before_provider_call():
    target = memory()
    target.analysis_input.window_end = NOW + timedelta(hours=1)
    svc, _, provider = service(target=target)
    with pytest.raises(AudienceSnapshotError):
        await preview(svc)
    provider.estimate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("tenant_id", str(uuid4())), ("run_id", uuid4()), ("news_id", "other")])
async def test_corrupt_or_cross_tenant_target_does_not_call_provider(field, value):
    target = memory()
    setattr(target, field, value)
    svc, _, provider = service(target=target)
    with pytest.raises(AudienceSnapshotError):
        await preview(svc)
    provider.estimate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [{"taxonomy_version": "old-version"}, {"include_topic_ids": ["unknown"]}])
async def test_bad_catalog_selection_fails_before_query(updates):
    svc, reader, provider = service()
    with pytest.raises(AudienceSelectionError):
        await preview(svc, selection(**updates))
    reader.get_analysis_memory.assert_not_awaited()
    provider.estimate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"tenant_id": str(uuid4())}, {"run_id": str(uuid4())}, {"news_id": "other"},
    {"taxonomy_version": "other"}, {"query_fingerprint": "f" * 64},
    {"window_start": NOW - timedelta(hours=3)}, {"source_mode": "synthetic"},
    {"is_complete": False}, {"watermark": NOW - timedelta(hours=3)},
    {"watermark": NOW + timedelta(days=365)},
    {"expires_at": NOW}, {"generated_at": NOW + timedelta(minutes=1)},
    {"generated_at": NOW - timedelta(hours=1)}, {"expires_at": NOW + timedelta(hours=1)},
    {"eligible_users": 38}, {"user_id": "must-not-cross-port"},
])
async def test_untrusted_aggregate_identity_freshness_and_shape_are_rechecked(changes):
    async def bad(query):
        payload = {**snapshot(query).model_dump(), **changes}
        return SimpleNamespace(model_dump=lambda **_kwargs: payload)
    provider = SimpleNamespace(estimate=AsyncMock(side_effect=bad), close=AsyncMock())
    svc, _, _ = service(provider=provider)
    with pytest.raises(AudienceSnapshotError):
        await preview(svc)


@pytest.mark.asyncio
async def test_zero_audience_observe_and_frozen_input_fingerprint():
    async def zero(query):
        return snapshot(query).model_copy(update={"matched_users": 0, "eligible_users": 0})
    svc, _, _ = service(provider=SimpleNamespace(estimate=zero, close=AsyncMock()))
    first, second = await preview(svc), await preview(svc)
    assert first.recommendation.action == "observe"
    assert first.fingerprint == second.fingerprint


@pytest.mark.asyncio
async def test_timeout_is_closed_without_synthetic_fallback():
    provider = SimpleNamespace(estimate=AsyncMock(side_effect=TimeoutError), close=AsyncMock())
    svc, _, _ = service(provider=provider)
    with pytest.raises(AudienceProviderError, match="timed out"):
        await preview(svc)
    assert provider.estimate.await_count == 1


def api_client(svc=None, roles="hot-news:decide", token="test-audience-token"):
    app = create_app()
    app.dependency_overrides[get_data_loop_gateway_token] = lambda: "test-audience-token"
    if svc is not None:
        app.dependency_overrides[get_audience_service] = lambda: svc
    return TestClient(app, headers={"Authorization": f"Bearer {token}",
        "X-Tenant-ID": str(TENANT), "X-User-ID": str(USER), "X-Hot-News-Roles": roles})


def test_api_topics_and_preview_run_through_real_service_with_local_port():
    provider = LocalAudienceProfileClient(CATALOG, environment="test", clock=lambda: NOW)
    svc, _, _ = service(provider=provider, mode="synthetic")
    client = api_client(svc, roles="hot-news:admin")
    topics = client.get("/api/v1/audience/topics")
    assert topics.status_code == 200 and topics.json()["source_mode"] == "synthetic"
    response = client.post(f"/api/v1/audience/runs/{RUN}/news/{NEWS}/preview",
                           json=selection().model_dump(mode="json"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["snapshot"]["source_mode"] == "synthetic"
    assert body["dispatch_enabled"] is False


@pytest.mark.parametrize("roles,token,expected", [
    ("hot-news:read", "test-audience-token", 403),
    ("hot-news:admin", "wrong", 401),
    ("", "test-audience-token", 403),
])
def test_api_permission_denial_never_calls_business_service(roles, token, expected):
    svc = SimpleNamespace(preview=AsyncMock())
    response = api_client(svc, roles, token).post(f"/api/v1/audience/runs/{RUN}/news/{NEWS}/preview",
                                               json=selection().model_dump(mode="json"))
    assert response.status_code == expected
    svc.preview.assert_not_awaited()


def test_api_rejects_body_identity_and_disabled_service():
    svc, _, provider = service()
    response = api_client(svc).post(f"/api/v1/audience/runs/{RUN}/news/{NEWS}/preview",
        json={**selection().model_dump(mode="json"), "tenant_id": str(uuid4())})
    assert response.status_code == 422
    provider.estimate.assert_not_awaited()
    assert api_client(roles="hot-news:read").get("/api/v1/audience/topics").status_code == 503
