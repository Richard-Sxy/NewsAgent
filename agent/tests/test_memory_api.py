"""用户 Memory API 的鉴权、身份收口与错误映射测试。

不访问真实数据库：写入/晋升/上下文服务与 session 均以替身注入，
验证 HTTP 层的可信网关头契约（复用网关 Token、独立 X-Memory-Roles）、
组织作用域只来自 Header，以及 409/422/503 的错误映射。
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.dependencies import (
    MemoryPermission,
    get_data_loop_gateway_token,
    get_memory_context_service,
    get_memory_promotion_service,
    get_memory_write_service,
    get_session,
)
from app.main import create_app
from app.schemas.user_memory import (
    LongTermMemoryCandidate,
    LongTermUserMemory,
    MemorySourceRef,
    ResolvedMemoryContext,
    ShortTermUserMemory,
    UserMemoryContent,
    UserMemoryScope,
)
from app.services.memory_write_application import (
    MemoryCandidateWriteOutcome,
    MemoryWriteConflictError,
    ShortTermMemoryWriteOutcome,
)
from app.services.memory_promotion_application import (
    MemoryPromotionOutcome,
    MemoryPromotionTargetNotFoundError,
)


TENANT_ID = uuid4()
USER_ID = uuid4()
TEAM_ID = "team-news"
GATEWAY_TOKEN = "test-memory-gateway-token"

NOW = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
EXPIRES_AT = NOW + timedelta(days=1)


def make_content() -> UserMemoryContent:
    return UserMemoryContent(
        kind="temporary_preference",
        key="output.language",
        value="zh-CN",
        summary="本次任务使用中文",
    )


def make_source_refs() -> tuple[MemorySourceRef, ...]:
    return (
        MemorySourceRef(
            source_type="user_message",
            source_id="message-1",
            captured_at=NOW,
        ),
    )


def make_short_term() -> ShortTermUserMemory:
    return ShortTermUserMemory(
        id=uuid4(),
        scope=UserMemoryScope(
            tenant_id=str(TENANT_ID),
            user_id=str(USER_ID),
            team_id=TEAM_ID,
        ),
        task_id="task-1",
        content=make_content(),
        origin="explicit_user",
        source_refs=make_source_refs(),
        confidence=1,
        created_at=NOW,
        expires_at=EXPIRES_AT,
    )


def make_candidate() -> LongTermMemoryCandidate:
    return LongTermMemoryCandidate(
        id=uuid4(),
        scope=UserMemoryScope(
            tenant_id=str(TENANT_ID),
            user_id=str(USER_ID),
            team_id=TEAM_ID,
        ),
        proposed_content=UserMemoryContent(
            kind="stable_preference",
            key="writing.tone",
            value="concise",
            summary="用户长期偏好简洁表达",
        ),
        origin="system_inference",
        source_refs=make_source_refs(),
        confidence=0.85,
        reason="多个任务重复观察到该偏好",
        created_at=NOW,
        expires_at=EXPIRES_AT,
    )


def make_long_term(candidate: LongTermMemoryCandidate) -> LongTermUserMemory:
    return LongTermUserMemory(
        id=uuid4(),
        scope=candidate.scope,
        content=candidate.proposed_content,
        origin="approved_candidate",
        source_refs=candidate.source_refs,
        confidence=candidate.confidence,
        confirmed_by=str(USER_ID),
        confirmed_at=NOW,
        valid_from=NOW,
        recorded_at=NOW,
    )


async def fake_session():
    yield SimpleNamespace()


def client_with(
    *,
    write_service=None,
    promotion_service=None,
    context_service=None,
    permission: MemoryPermission | None = MemoryPermission.ADMIN,
    bearer_token: str | None = GATEWAY_TOKEN,
    include_identity: bool = True,
    include_team: bool = True,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_data_loop_gateway_token] = (
        lambda: GATEWAY_TOKEN
    )
    app.dependency_overrides[get_session] = fake_session
    if write_service is not None:
        app.dependency_overrides[get_memory_write_service] = (
            lambda: write_service
        )
    if promotion_service is not None:
        app.dependency_overrides[get_memory_promotion_service] = (
            lambda: promotion_service
        )
    if context_service is not None:
        app.dependency_overrides[get_memory_context_service] = (
            lambda: context_service
        )

    headers: dict[str, str] = {}
    if bearer_token is not None:
        headers["Authorization"] = f"Bearer {bearer_token}"
    if include_identity:
        headers["X-Tenant-ID"] = str(TENANT_ID)
        headers["X-User-ID"] = str(USER_ID)
    if include_team:
        headers["X-Team-ID"] = TEAM_ID
    if permission is not None:
        headers["X-Memory-Roles"] = permission.value
    return TestClient(app, headers=headers)


def short_term_payload(**overrides) -> dict:
    payload = {
        "task_id": "task-1",
        "content": {
            "kind": "temporary_preference",
            "key": "output.language",
            "value": "zh-CN",
            "summary": "本次任务使用中文",
        },
        "origin": "explicit_user",
        "source_refs": [
            {
                "source_type": "user_message",
                "source_id": "message-1",
                "captured_at": NOW.isoformat(),
            }
        ],
        "confidence": 1,
        "expires_at": EXPIRES_AT.isoformat(),
        "idempotency_key": "short-memory-request-1",
    }
    payload.update(overrides)
    return payload


def test_create_short_term_uses_gateway_scope() -> None:
    memory = make_short_term()
    service = SimpleNamespace(
        create_short_term=AsyncMock(
            return_value=ShortTermMemoryWriteOutcome(memory, True)
        )
    )

    response = client_with(
        write_service=service,
        permission=MemoryPermission.WRITE,
    ).post("/api/v1/memory/short-term", json=short_term_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["memory"]["scope"]["tenant_id"] == str(TENANT_ID)
    assert body["memory"]["scope"]["user_id"] == str(USER_ID)
    assert body["memory"]["scope"]["team_id"] == TEAM_ID

    command = service.create_short_term.await_args.kwargs["command"]
    assert command.scope.tenant_id == str(TENANT_ID)
    assert command.scope.user_id == str(USER_ID)
    assert command.scope.team_id == TEAM_ID
    assert command.idempotency_key == "short-memory-request-1"


def test_create_short_term_rejects_spoofed_identity_in_body() -> None:
    service = SimpleNamespace(create_short_term=AsyncMock())

    response = client_with(
        write_service=service,
        permission=MemoryPermission.WRITE,
    ).post(
        "/api/v1/memory/short-term",
        json=short_term_payload(scope={"tenant_id": str(uuid4())}),
    )

    assert response.status_code == 422
    service.create_short_term.assert_not_awaited()


def test_create_short_term_requires_write_permission() -> None:
    service = SimpleNamespace(create_short_term=AsyncMock())

    response = client_with(
        write_service=service,
        permission=MemoryPermission.READ,
    ).post("/api/v1/memory/short-term", json=short_term_payload())

    assert response.status_code == 403
    service.create_short_term.assert_not_awaited()


def test_create_short_term_rejects_missing_bearer_token() -> None:
    service = SimpleNamespace(create_short_term=AsyncMock())

    response = client_with(
        write_service=service,
        bearer_token=None,
    ).post("/api/v1/memory/short-term", json=short_term_payload())

    assert response.status_code == 401
    service.create_short_term.assert_not_awaited()


def test_create_short_term_rejects_missing_roles_header() -> None:
    service = SimpleNamespace(create_short_term=AsyncMock())

    response = client_with(
        write_service=service,
        permission=None,
    ).post("/api/v1/memory/short-term", json=short_term_payload())

    assert response.status_code == 403


def test_create_short_term_maps_conflict_to_409() -> None:
    service = SimpleNamespace(
        create_short_term=AsyncMock(
            side_effect=MemoryWriteConflictError("幂等键冲突")
        )
    )

    response = client_with(
        write_service=service,
        permission=MemoryPermission.WRITE,
    ).post("/api/v1/memory/short-term", json=short_term_payload())

    assert response.status_code == 409


def test_propose_candidate_returns_candidate() -> None:
    candidate = make_candidate()
    service = SimpleNamespace(
        propose_long_term=AsyncMock(
            return_value=MemoryCandidateWriteOutcome(candidate, True)
        )
    )

    response = client_with(
        write_service=service,
        permission=MemoryPermission.WRITE,
    ).post(
        "/api/v1/memory/candidates",
        json={
            "proposed_content": {
                "kind": "stable_preference",
                "key": "writing.tone",
                "value": "concise",
                "summary": "用户长期偏好简洁表达",
            },
            "origin": "system_inference",
            "source_refs": [
                {
                    "source_type": "user_message",
                    "source_id": "message-1",
                    "captured_at": NOW.isoformat(),
                }
            ],
            "confidence": 0.85,
            "reason": "多个任务重复观察到该偏好",
            "expires_at": EXPIRES_AT.isoformat(),
            "idempotency_key": "candidate-request-1",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["candidate"]["status"] == "pending"
    assert body["candidate"]["scope"]["user_id"] == str(USER_ID)


def test_promote_candidate_requires_approve_permission() -> None:
    service = SimpleNamespace(promote=AsyncMock())

    response = client_with(
        promotion_service=service,
        permission=MemoryPermission.WRITE,
    ).post(
        f"/api/v1/memory/candidates/{uuid4()}/promote",
        json={"expected_version": 1, "idempotency_key": "promote-request-1"},
    )

    assert response.status_code == 403
    service.promote.assert_not_awaited()


def test_promote_candidate_returns_long_term_memory() -> None:
    candidate = make_candidate()
    memory = make_long_term(candidate)
    service = SimpleNamespace(
        promote=AsyncMock(return_value=MemoryPromotionOutcome(memory, True))
    )

    response = client_with(
        promotion_service=service,
        permission=MemoryPermission.APPROVE,
    ).post(
        f"/api/v1/memory/candidates/{candidate.id}/promote",
        json={"expected_version": 1, "idempotency_key": "promote-request-1"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["memory"]["origin"] == "approved_candidate"

    command = service.promote.await_args.kwargs["command"]
    assert command.tenant_id == str(TENANT_ID)
    assert command.user_id == str(USER_ID)
    assert command.approved_by == str(USER_ID)
    assert command.candidate_id == candidate.id


def test_promote_candidate_maps_not_found_to_404() -> None:
    service = SimpleNamespace(
        promote=AsyncMock(
            side_effect=MemoryPromotionTargetNotFoundError("候选不存在")
        )
    )

    response = client_with(
        promotion_service=service,
        permission=MemoryPermission.APPROVE,
    ).post(
        f"/api/v1/memory/candidates/{uuid4()}/promote",
        json={"expected_version": 1, "idempotency_key": "promote-request-1"},
    )

    assert response.status_code == 404


def test_resolve_context_passes_principal_scope() -> None:
    context = ResolvedMemoryContext(
        tenant_id=str(TENANT_ID),
        user_id=str(USER_ID),
        task_id="task-1",
        team_id=TEAM_ID,
        resolved_at=NOW,
        memories=(),
    )
    service = SimpleNamespace(
        resolve_for_task=AsyncMock(return_value=context)
    )

    response = client_with(
        context_service=service,
        permission=MemoryPermission.READ,
    ).get("/api/v1/memory/context", params={"task_id": "task-1"})

    assert response.status_code == 200
    assert response.json()["context"]["memories"] == []
    kwargs = service.resolve_for_task.await_args.kwargs
    assert kwargs["tenant_id"] == str(TENANT_ID)
    assert kwargs["user_id"] == str(USER_ID)
    assert kwargs["team_id"] == TEAM_ID


def test_context_rejects_other_module_roles_header() -> None:
    """Data Loop 角色头不能替代 Memory 独立角色头。"""

    app = create_app()
    app.dependency_overrides[get_data_loop_gateway_token] = (
        lambda: GATEWAY_TOKEN
    )
    app.dependency_overrides[get_session] = fake_session
    app.dependency_overrides[get_memory_context_service] = (
        lambda: SimpleNamespace(resolve_for_task=AsyncMock())
    )
    client = TestClient(
        app,
        headers={
            "Authorization": f"Bearer {GATEWAY_TOKEN}",
            "X-Tenant-ID": str(TENANT_ID),
            "X-User-ID": str(USER_ID),
            "X-Data-Loop-Roles": "data-loop:admin",
        },
    )

    response = client.get(
        "/api/v1/memory/context", params={"task_id": "task-1"}
    )

    assert response.status_code == 403
