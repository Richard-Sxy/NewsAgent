"""Scaled preview/run identities cannot reuse another warehouse or size."""

from pathlib import Path

import pytest

from app.sql_assistant.hot_news_binding import HotNewsSqlBindingError
from app.sql_assistant.service import SqlAssistantError, SqlAssistantService
from app.sql_assistant.warehouse import warehouse_contract
from tests.test_hot_news_sql_binding import scope
from tests.test_sql_assistant_service import (
    MemorySnapshots, RecordingAgent, RecordingWarehouse, TENANT, USER, request,
)


def scaled_service(*, size=120, scenarios="text2sql-scenes.enterprise-v2.yml"):
    agent, store, warehouse = RecordingAgent(), MemorySnapshots(), RecordingWarehouse()
    agent.intent = agent.intent.model_copy(update={"row_limit": 100})
    return SqlAssistantService(
        agent=agent, store=store, warehouse_factory=lambda tenant: warehouse,
        scenarios_path=str(Path(__file__).resolve().parents[1] / "deploy" / scenarios),
        model_provider="local-deterministic", dataset_profile="enterprise-v2",
        news_per_tenant=size,
    )


def test_existing_v1_scope_hash_is_unchanged():
    assert scope() == "16e3135ceca3e393131f415cd9399f95897f3ba0c7621e5b75d7de45444ed808"


def test_scaled_scope_freezes_size_version_and_hash():
    contract = warehouse_contract("enterprise-v2")
    identity = {"dataset_profile": "enterprise-v2", "dataset_version": "scaled-test-v1",
                "dataset_sha256": "a" * 64, "news_per_tenant": 1200}
    kwargs = {"warehouse_schema_version": contract.version, "schema_sha256": contract.sha256,
              "dataset_identity": identity}
    digest = scope(**kwargs)
    for change in ({"news_per_tenant": 120}, {"dataset_version": "scaled-test-v2"},
                   {"dataset_sha256": "b" * 64}):
        assert digest != scope(**{**kwargs, "dataset_identity": {**identity, **change}})
    with pytest.raises(HotNewsSqlBindingError, match="identity"):
        scope(warehouse_schema_version=contract.version)
    with pytest.raises(HotNewsSqlBindingError, match="profile or size"):
        scope(**{**kwargs, "dataset_identity": {**identity, "news_per_tenant": True}})


@pytest.mark.asyncio
async def test_scaled_preview_uses_active_schema_and_rejects_size_drift():
    service = scaled_service()
    preview = await service.preview(request(question="查询点击量最高的前100条新闻"), tenant_id=TENANT, user_id=USER)
    assert preview.schema_version == service.contract.version == "news-warehouse-v2"
    assert preview.schema_sha256 == service.contract.sha256
    snapshot = service.store.snapshots[preview.query_id]
    assert snapshot["dataset_contract"]["news_per_tenant"] == 120
    assert service.agent.calls[0]["payload"]["schema_version"] == "news-warehouse-v2"
    await service.execute(preview.query_id, tenant_id=TENANT, user_id=USER)
    service.news_per_tenant = 1200
    with pytest.raises(SqlAssistantError, match="规模或版本"):
        await service.execute(preview.query_id, tenant_id=TENANT, user_id=USER)


def test_scaled_profile_rejects_v1_scenario_file_and_invalid_size():
    with pytest.raises(SqlAssistantError, match="格式版本"):
        scaled_service(scenarios="text2sql-scenes.local.yml").config()
    with pytest.raises(ValueError, match="120"):
        scaled_service(size=12)
