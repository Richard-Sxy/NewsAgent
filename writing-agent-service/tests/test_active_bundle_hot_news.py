from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app.services.active_bundle_hot_news as module
from app.domain.errors import HotNewsDataQualityError
from app.services.active_bundle_hot_news import (
    ActiveProductionBundleHotNewsService,
)
from app.repositories.production_bundle import (
    ProductionBundleDataCorruptedError,
)
from app.services.hot_news_orchestration import (
    HotNewsOrchestrationPolicy,
    HotNewsRunRequest,
)
from tests.test_hot_news_orchestration import START
from tests.test_production_bundle import active_bundle


class Database:
    @asynccontextmanager
    async def session(self):
        yield object()


@pytest.mark.asyncio
async def test_online_run_resolves_active_bundle_and_builds_one_snapshot_runtime(
    monkeypatch,
) -> None:
    active = active_bundle()
    repository = SimpleNamespace(
        get_active_bundle=AsyncMock(return_value=active)
    )
    monkeypatch.setattr(
        module,
        "PostgresProductionBundleRepository",
        lambda session: repository,
    )
    downstream_result = object()
    downstream = SimpleNamespace(run=AsyncMock(return_value=downstream_result))
    service_factory = Mock(return_value=downstream)
    monkeypatch.setattr(module, "HotNewsOrchestrationService", service_factory)
    runner = object()
    registry = SimpleNamespace(
        ensure_supported=Mock(),
        create=Mock(return_value=runner),
    )
    service = ActiveProductionBundleHotNewsService(
        database=Database(),
        runtime_registry=registry,
        behavior_data_source=object(),
        baseline_provider=object(),
        content_repository=object(),
        knowledge_search=object(),
        policy_template=HotNewsOrchestrationPolicy(
            production_bundle_version="startup-value",
        ),
    )
    request = HotNewsRunRequest(
        tenant_id=active.tenant_id,
        window_start=START,
        window_end=START + timedelta(hours=1),
        production_bundle_version=active.bundle_version,
    )

    result = await service.run(request)

    assert result is downstream_result
    registry.ensure_supported.assert_called_once_with(active.spec)
    registry.create.assert_called_once_with(active.spec)
    built = service_factory.call_args.kwargs
    assert built["policy"].production_bundle_version == active.bundle_version
    assert built["analysis_service"].runner is runner
    assert (
        built["analysis_service"].input_builder.policy_version
        == active.spec.analysis_prompt_version
    )


@pytest.mark.asyncio
async def test_online_run_fails_closed_when_request_targets_stale_bundle(
    monkeypatch,
) -> None:
    active = active_bundle()
    monkeypatch.setattr(
        module,
        "PostgresProductionBundleRepository",
        lambda session: SimpleNamespace(
            get_active_bundle=AsyncMock(return_value=active)
        ),
    )
    service = ActiveProductionBundleHotNewsService(
        database=Database(),
        runtime_registry=SimpleNamespace(),
        behavior_data_source=object(),
        baseline_provider=object(),
        content_repository=object(),
        knowledge_search=object(),
        policy_template=HotNewsOrchestrationPolicy(
            production_bundle_version="startup-value",
        ),
    )
    request = HotNewsRunRequest(
        tenant_id=active.tenant_id,
        window_start=START,
        window_end=START + timedelta(hours=1),
        production_bundle_version="stale-v0",
    )

    with pytest.raises(HotNewsDataQualityError, match="currently active"):
        await service.run(request)


@pytest.mark.asyncio
async def test_corrupted_active_bundle_is_non_retryable_data_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        module,
        "PostgresProductionBundleRepository",
        lambda session: SimpleNamespace(
            get_active_bundle=AsyncMock(
                side_effect=ProductionBundleDataCorruptedError("bad snapshot")
            )
        ),
    )
    service = ActiveProductionBundleHotNewsService(
        database=Database(),
        runtime_registry=SimpleNamespace(),
        behavior_data_source=object(),
        baseline_provider=object(),
        content_repository=object(),
        knowledge_search=object(),
        policy_template=HotNewsOrchestrationPolicy(
            production_bundle_version="startup-value",
        ),
    )
    request = HotNewsRunRequest(
        tenant_id="tenant-1",
        window_start=START,
        window_end=START + timedelta(hours=1),
        production_bundle_version="bundle-v1",
    )

    with pytest.raises(HotNewsDataQualityError, match="corrupted") as captured:
        await service.run(request)

    assert captured.value.retryable is False
