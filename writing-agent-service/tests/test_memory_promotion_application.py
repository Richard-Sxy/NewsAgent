from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

import app.services.memory_promotion_application as application_module
from app.repositories.user_memory import PostgresUserMemoryRepository
from app.schemas.user_memory import PromoteMemoryCandidateCommand
from app.services.memory_promotion_application import (
    MemoryPromotionApplicationService,
    MemoryPromotionConflictError,
    MemoryPromotionPersistenceError,
    MemoryPromotionTargetNotFoundError,
    build_command_fingerprint,
)


NOW = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)


def command(**updates) -> PromoteMemoryCandidateCommand:
    values = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "candidate_id": uuid4(),
        "approved_by": "operator-1",
        "expected_version": 1,
        "idempotency_key": "promotion-request-1",
    }
    values.update(updates)
    return PromoteMemoryCandidateCommand(**values)


def repository_mock(monkeypatch):
    repository = create_autospec(
        PostgresUserMemoryRepository,
        instance=True,
    )
    monkeypatch.setattr(
        application_module,
        "PostgresUserMemoryRepository",
        lambda session: repository,
    )
    return repository


def service_with_result(memory):
    approved_candidate = object()
    domain_service = create_autospec(
        application_module.MemoryPromotionService,
        instance=True,
    )
    domain_service.promote.return_value = SimpleNamespace(
        approved_candidate=approved_candidate,
        long_term_memory=memory,
    )
    return MemoryPromotionApplicationService(domain_service), domain_service


@pytest.mark.asyncio
async def test_promotes_candidate_in_expected_write_order(monkeypatch):
    request = command()
    candidate = SimpleNamespace(id=request.candidate_id)
    memory = SimpleNamespace(id=uuid4())
    promotion_request = SimpleNamespace(id=uuid4())
    repository = repository_mock(monkeypatch)
    repository.get_promotion_request_for_update.return_value = None
    repository.get_candidate_for_update.return_value = candidate
    repository.reserve_promotion_request.return_value = promotion_request
    repository.mark_candidate_approved.return_value = True
    repository.complete_promotion_request.return_value = True
    service, domain_service = service_with_result(memory)

    outcome = await service.promote(
        object(),
        command=request,
        now=NOW,
    )

    assert outcome.memory is memory
    assert outcome.created is True
    repository.get_promotion_request_for_update.assert_awaited_once_with(
        tenant_id=request.tenant_id,
        idempotency_key=request.idempotency_key,
    )
    repository.get_candidate_for_update.assert_awaited_once_with(
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        candidate_id=request.candidate_id,
    )
    assert domain_service.promote.call_args.kwargs["candidate"] is candidate
    repository.insert_long_term_memory.assert_awaited_once_with(
        memory=memory,
        candidate_id=candidate.id,
    )
    repository.complete_promotion_request.assert_awaited_once_with(
        request_id=promotion_request.id,
        tenant_id=request.tenant_id,
        memory_id=memory.id,
    )


@pytest.mark.asyncio
async def test_completed_request_is_replayed_without_writes(monkeypatch):
    request = command()
    memory = SimpleNamespace(id=uuid4())
    existing = SimpleNamespace(
        request_fingerprint=build_command_fingerprint(request),
        status="completed",
        resulting_memory_id=memory.id,
    )
    repository = repository_mock(monkeypatch)
    repository.get_promotion_request_for_update.return_value = existing
    repository.get_long_term.return_value = memory
    service, domain_service = service_with_result(memory)

    outcome = await service.promote(
        object(),
        command=request,
        now=NOW,
    )

    assert outcome.memory is memory
    assert outcome.created is False
    domain_service.promote.assert_not_called()
    repository.reserve_promotion_request.assert_not_awaited()
    repository.insert_long_term_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_reused_idempotency_key_with_other_content_conflicts(
    monkeypatch,
):
    request = command()
    existing = SimpleNamespace(
        request_fingerprint="0" * 64,
        status="completed",
        resulting_memory_id=uuid4(),
    )
    repository = repository_mock(monkeypatch)
    repository.get_promotion_request_for_update.return_value = existing
    service, _ = service_with_result(SimpleNamespace(id=uuid4()))

    with pytest.raises(
        MemoryPromotionConflictError,
        match="idempotency_key",
    ):
        await service.promote(object(), command=request, now=NOW)


@pytest.mark.asyncio
async def test_missing_candidate_is_hidden_as_not_found(monkeypatch):
    request = command()
    repository = repository_mock(monkeypatch)
    repository.get_promotion_request_for_update.return_value = None
    repository.get_candidate_for_update.return_value = None
    service, domain_service = service_with_result(
        SimpleNamespace(id=uuid4())
    )

    with pytest.raises(
        MemoryPromotionTargetNotFoundError,
        match="候选不存在",
    ):
        await service.promote(object(), command=request, now=NOW)

    domain_service.promote.assert_not_called()
    repository.reserve_promotion_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_replayed_result_is_persistence_error(monkeypatch):
    request = command()
    existing = SimpleNamespace(
        request_fingerprint=build_command_fingerprint(request),
        status="completed",
        resulting_memory_id=uuid4(),
    )
    repository = repository_mock(monkeypatch)
    repository.get_promotion_request_for_update.return_value = existing
    repository.get_long_term.return_value = None
    service, _ = service_with_result(SimpleNamespace(id=uuid4()))

    with pytest.raises(
        MemoryPromotionPersistenceError,
        match="无法读取",
    ):
        await service.promote(object(), command=request, now=NOW)


@pytest.mark.asyncio
async def test_candidate_compare_and_set_conflict_stops_insert(monkeypatch):
    request = command()
    candidate = SimpleNamespace(id=request.candidate_id)
    memory = SimpleNamespace(id=uuid4())
    repository = repository_mock(monkeypatch)
    repository.get_promotion_request_for_update.return_value = None
    repository.get_candidate_for_update.return_value = candidate
    repository.reserve_promotion_request.return_value = SimpleNamespace(
        id=uuid4()
    )
    repository.mark_candidate_approved.return_value = False
    service, _ = service_with_result(memory)

    with pytest.raises(
        MemoryPromotionConflictError,
        match="候选版本或状态",
    ):
        await service.promote(object(), command=request, now=NOW)

    repository.insert_long_term_memory.assert_not_awaited()
    repository.complete_promotion_request.assert_not_awaited()
