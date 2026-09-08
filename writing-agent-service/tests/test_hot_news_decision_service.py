from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.models.hot_news_decision import HotNewsDecision
from app.schemas.hot_news_decision import RecordHotNewsDecisionCommand
from app.services.hot_news_decision import (
    HotNewsDecisionConflictError,
    HotNewsDecisionService,
    HotNewsDecisionTargetNotFoundError,
)


class ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def command(**overrides) -> RecordHotNewsDecisionCommand:
    values = {
        "run_id": uuid4(),
        "news_id": "news-1",
        "decision_type": "accepted",
        "reason": "具有运营价值",
        "idempotency_key": "decision-request-1",
    }
    values.update(overrides)
    return RecordHotNewsDecisionCommand(**values)


def decision_for(
    request: RecordHotNewsDecisionCommand,
    *,
    tenant_id: str = "tenant-1",
    operator_id: str = "operator-1",
    **overrides,
) -> HotNewsDecision:
    values = {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "run_id": request.run_id,
        "news_id": request.news_id,
        "decision_type": request.decision_type,
        "reason": request.reason,
        "correction_payload": request.correction_payload,
        "operator_id": operator_id,
        "idempotency_key": request.idempotency_key,
        "supersedes_decision_id": request.supersedes_decision_id,
    }
    values.update(overrides)
    return HotNewsDecision(**values)


def service_with_results(*results, memory=object()):
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[ScalarResult(result) for result in results]
        )
    )

    @asynccontextmanager
    async def open_session():
        yield session

    memory_store = SimpleNamespace(
        get_analysis_memory=AsyncMock(return_value=memory)
    )
    service = HotNewsDecisionService(
        database=SimpleNamespace(session=open_session),
        memory_store=memory_store,
    )
    return service, session, memory_store


@pytest.mark.asyncio
async def test_target_lookup_is_tenant_scoped_and_hides_cross_tenant_target():
    request = command()
    service, session, memory_store = service_with_results(memory=None)

    with pytest.raises(
        HotNewsDecisionTargetNotFoundError,
        match="热点决策目标不存在",
    ):
        await service.record_decision(
            tenant_id="tenant-1",
            operator_id="operator-1",
            command=request,
        )

    memory_store.get_analysis_memory.assert_awaited_once_with(
        tenant_id="tenant-1",
        run_id=request.run_id,
        news_id=request.news_id,
    )
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant_id", "operator_id", "error_message"),
    [
        ("   ", "operator-1", "tenant_id 不能为空"),
        ("tenant-1", "   ", "operator_id 不能为空"),
    ],
)
async def test_rejects_blank_security_context(
    tenant_id,
    operator_id,
    error_message,
):
    service, session, memory_store = service_with_results()

    with pytest.raises(ValueError, match=error_message):
        await service.record_decision(
            tenant_id=tenant_id,
            operator_id=operator_id,
            command=command(),
        )

    memory_store.get_analysis_memory.assert_not_awaited()
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_idempotent_replay_returns_existing_decision():
    request = command()
    existing = decision_for(request)
    service, session, _ = service_with_results(None, existing)

    result, created = await service.record_decision(
        tenant_id="tenant-1",
        operator_id="operator-1",
        command=request,
    )

    assert result is existing
    assert created is False
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_content_is_rejected():
    request = command()
    existing = decision_for(request, reason="不同的原因")
    service, _, _ = service_with_results(None, existing)

    with pytest.raises(HotNewsDecisionConflictError):
        await service.record_decision(
            tenant_id="tenant-1",
            operator_id="operator-1",
            command=request,
        )


@pytest.mark.asyncio
async def test_superseded_decision_must_match_tenant_run_and_news():
    superseded_id = uuid4()
    request = command(supersedes_decision_id=superseded_id)
    service, session, _ = service_with_results(None)

    with pytest.raises(HotNewsDecisionTargetNotFoundError):
        await service.record_decision(
            tenant_id="tenant-1",
            operator_id="operator-1",
            command=request,
        )

    assert session.execute.await_count == 1
    statement = session.execute.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    params = compiled.params
    assert superseded_id in params.values()
    assert "tenant-1" in params.values()
    assert request.run_id in params.values()
    assert request.news_id in params.values()


@pytest.mark.asyncio
async def test_matching_superseded_decision_allows_insert():
    superseded_id = uuid4()
    request = command(supersedes_decision_id=superseded_id)
    superseded = decision_for(request, id=superseded_id)
    inserted = decision_for(request)
    service, session, _ = service_with_results(superseded, inserted)

    result, created = await service.record_decision(
        tenant_id="tenant-1",
        operator_id="operator-1",
        command=request,
    )

    assert result is inserted
    assert created is True
    assert session.execute.await_count == 2
