"""Optional API lifespan assembly, without DB/network or dotenv access."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from app.audience.contracts import AudienceProviderError
from app.config import Settings
import app.main as main_module

ROOT = Path(__file__).resolve().parents[1]


def settings(**updates):
    values = dict(_env_file=None, environment="e2e",
        database_url="postgresql+psycopg://test:test@localhost:5432/test",
        redis_url="redis://localhost:6379/0", temporal_address="localhost:7233",
        temporal_namespace="test", artifact_bucket="test-only",
        model_runtime_config_path=str(ROOT / "deploy/model-runtime.local.yml"),
        audience_enabled=True, audience_backend="synthetic",
        audience_taxonomy_path=str(ROOT / "deploy/audience-topics.local.yml"))
    return Settings(**{**values, **updates})


def infrastructure(monkeypatch, config):
    database, redis = SimpleNamespace(close=AsyncMock()), SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(main_module, "get_settings", lambda: config)
    monkeypatch.setattr(main_module, "Database", lambda _: database)
    monkeypatch.setattr(main_module, "Client", SimpleNamespace(connect=AsyncMock(return_value=object())))
    monkeypatch.setattr(main_module, "Redis", SimpleNamespace(from_url=lambda *args, **kwargs: redis))
    for name in ("S3ArtifactStore", "S3EvaluationDatasetArtifactStore", "S3DataLoopArtifactStore"):
        monkeypatch.setattr(main_module, name, lambda _: object())
    monkeypatch.setattr(main_module, "build_knowledge_search_index", lambda **kwargs: None)
    monkeypatch.setattr(main_module, "close_knowledge_search_index", AsyncMock())
    return database, redis


@pytest.mark.asyncio
async def test_lifespan_builds_opted_in_service_and_closes_client(monkeypatch):
    config = settings(audience_max_snapshot_age_seconds=60)
    database, redis = infrastructure(monkeypatch, config)
    app = main_module.create_app()
    async with main_module._business_lifespan(app):
        service = app.state.audience_service
        assert service.source_mode == "synthetic" and service.max_snapshot_age_seconds == 60
        assert service.client._ttl_seconds == 60
        assert service.catalog.taxonomy_version == "news-interest-demo-v1"
    with pytest.raises(AudienceProviderError, match="closed"):
        await service.client.estimate(None)
    database.close.assert_awaited_once()
    redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_feature_preserves_api_lifespan(monkeypatch):
    infrastructure(monkeypatch, settings(audience_enabled=False))
    app = main_module.create_app()
    async with main_module._business_lifespan(app):
        assert app.state.audience_service is None


@pytest.mark.asyncio
async def test_enterprise_draft_catalog_fails_before_connecting_provider(monkeypatch):
    database, redis = infrastructure(monkeypatch, settings(audience_backend="enterprise_http"))
    with pytest.raises(ValueError, match="approved"):
        async with main_module._business_lifespan(main_module.create_app()):
            pytest.fail("unapproved enterprise catalog cannot start")
    database.close.assert_awaited_once()
    redis.aclose.assert_awaited_once()


def test_local_overlay_explicitly_selects_the_synthetic_port():
    overlay = yaml.safe_load((ROOT / "deploy/docker-compose.audience.local.yml").read_text())
    environment = overlay["services"]["api"]["environment"]
    assert environment["AUDIENCE_BACKEND"] == "synthetic"
    assert environment["AUDIENCE_TAXONOMY_PATH"] == "/app/deploy/audience-topics.local.yml"
    assert overlay["services"]["api"]["volumes"] == [
        "./audience-topics.local.yml:/app/deploy/audience-topics.local.yml:ro"]
