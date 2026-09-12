"""用户 Memory 运营控制台 API：长短期记忆读写、候选晋升与上下文解析。

鉴权模型：与 Data Loop、热点复用同一个网关共享 Bearer Token
（DATA_LOOP_GATEWAY_TOKEN），但权限串来自独立的 ``X-Memory-Roles`` 头。
租户、用户和组织作用域只信任网关注入的 ``X-Tenant-ID`` / ``X-User-ID`` /
``X-Team-ID`` / ``X-Section-ID`` / ``X-Role-ID``，绝不从请求体读取。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    DataLoopPrincipal,
    MemoryPermission,
    get_memory_context_service,
    get_memory_promotion_service,
    get_memory_write_service,
    get_session,
    require_memory_permission,
)
from app.repositories.user_memory import (
    PostgresUserMemoryRepository,
    UserMemoryDataCorruptedError,
)
from app.schemas.user_memory import (
    CreateShortTermMemoryCommand,
    PromoteMemoryCandidateCommand,
    ProposeLongTermMemoryCommand,
    UserMemoryScope,
)
from app.schemas.user_memory_api import (
    CreateShortTermMemoryRequest,
    LongTermMemoryListResponse,
    LongTermMemoryWriteResponse,
    MemoryCandidateWriteResponse,
    PromoteMemoryCandidateRequest,
    ProposeLongTermMemoryRequest,
    ResolvedMemoryContextResponse,
    ShortTermMemoryListResponse,
    ShortTermMemoryWriteResponse,
)
from app.services.memory_context_application import (
    MemoryContextApplicationService,
    MemoryContextPersistenceError,
)
from app.services.memory_promotion import MemoryPromotionRejectedError
from app.services.memory_promotion_application import (
    MemoryPromotionApplicationService,
    MemoryPromotionConflictError,
    MemoryPromotionPersistenceError,
    MemoryPromotionTargetNotFoundError,
)
from app.services.memory_write_application import (
    MemoryWriteApplicationService,
    MemoryWriteConflictError,
    MemoryWritePersistenceError,
)


router = APIRouter(prefix="/api/v1/memory", tags=["user-memory"])

ReadPrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_memory_permission(MemoryPermission.READ)),
]
WritePrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_memory_permission(MemoryPermission.WRITE)),
]
ApprovePrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_memory_permission(MemoryPermission.APPROVE)),
]


@router.post(
    "/short-term",
    response_model=ShortTermMemoryWriteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_short_term_memory(
    request: CreateShortTermMemoryRequest,
    principal: WritePrincipal,
    session: AsyncSession = Depends(get_session),
    service: MemoryWriteApplicationService = Depends(
        get_memory_write_service
    ),
) -> ShortTermMemoryWriteResponse:
    """创建有任务边界和有效期的短期记忆；幂等键重放返回原记录。"""

    try:
        outcome = await service.create_short_term(
            session,
            command=CreateShortTermMemoryCommand(
                scope=_scope_from_principal(principal),
                task_id=request.task_id,
                content=request.content,
                origin=request.origin,
                source_refs=request.source_refs,
                confidence=request.confidence,
                expires_at=request.expires_at,
                idempotency_key=request.idempotency_key,
            ),
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:
        _raise_memory_http_error(exc)
    return ShortTermMemoryWriteResponse(
        memory=outcome.memory,
        created=outcome.created,
    )


@router.post(
    "/candidates",
    response_model=MemoryCandidateWriteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def propose_long_term_candidate(
    request: ProposeLongTermMemoryRequest,
    principal: WritePrincipal,
    session: AsyncSession = Depends(get_session),
    service: MemoryWriteApplicationService = Depends(
        get_memory_write_service
    ),
) -> MemoryCandidateWriteResponse:
    """提交待人工审批的长期候选，不直接写长期记忆。"""

    try:
        outcome = await service.propose_long_term(
            session,
            command=ProposeLongTermMemoryCommand(
                scope=_scope_from_principal(principal),
                proposed_content=request.proposed_content,
                origin=request.origin,
                source_refs=request.source_refs,
                confidence=request.confidence,
                reason=request.reason,
                expires_at=request.expires_at,
                idempotency_key=request.idempotency_key,
            ),
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:
        _raise_memory_http_error(exc)
    return MemoryCandidateWriteResponse(
        candidate=outcome.candidate,
        created=outcome.created,
    )


@router.post(
    "/candidates/{candidate_id}/promote",
    response_model=LongTermMemoryWriteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def promote_long_term_candidate(
    candidate_id: UUID,
    request: PromoteMemoryCandidateRequest,
    principal: ApprovePrincipal,
    session: AsyncSession = Depends(get_session),
    service: MemoryPromotionApplicationService = Depends(
        get_memory_promotion_service
    ),
) -> LongTermMemoryWriteResponse:
    """由具备审批权限的员工批准候选并生成生效的长期记忆。"""

    try:
        outcome = await service.promote(
            session,
            command=PromoteMemoryCandidateCommand(
                tenant_id=str(principal.tenant_id),
                user_id=str(principal.user_id),
                candidate_id=candidate_id,
                approved_by=str(principal.user_id),
                expected_version=request.expected_version,
                idempotency_key=request.idempotency_key,
                valid_until=request.valid_until,
                supersedes_memory_id=request.supersedes_memory_id,
            ),
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:
        _raise_memory_http_error(exc)
    return LongTermMemoryWriteResponse(
        memory=outcome.memory,
        created=outcome.created,
    )


@router.get("/context", response_model=ResolvedMemoryContextResponse)
async def resolve_memory_context(
    principal: ReadPrincipal,
    task_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: AsyncSession = Depends(get_session),
    service: MemoryContextApplicationService = Depends(
        get_memory_context_service
    ),
) -> ResolvedMemoryContextResponse:
    """解析当前用户在指定任务下的长短期记忆运行上下文。"""

    try:
        context = await service.resolve_for_task(
            session,
            tenant_id=str(principal.tenant_id),
            user_id=str(principal.user_id),
            task_id=task_id,
            now=datetime.now(timezone.utc),
            team_id=principal.team_id,
            section_id=principal.section_id,
            role_id=principal.role_id,
        )
    except Exception as exc:
        _raise_memory_http_error(exc)
    return ResolvedMemoryContextResponse(context=context)


@router.get("/short-term", response_model=ShortTermMemoryListResponse)
async def list_short_term_memories(
    principal: ReadPrincipal,
    task_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: AsyncSession = Depends(get_session),
) -> ShortTermMemoryListResponse:
    """列出当前用户指定任务内仍然有效的短期记忆。"""

    try:
        memories = await PostgresUserMemoryRepository(
            session
        ).list_active_short_term(
            tenant_id=str(principal.tenant_id),
            user_id=str(principal.user_id),
            task_id=task_id,
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:
        _raise_memory_http_error(exc)
    return ShortTermMemoryListResponse(memories=tuple(memories))


@router.get("/long-term", response_model=LongTermMemoryListResponse)
async def list_long_term_memories(
    principal: ReadPrincipal,
    session: AsyncSession = Depends(get_session),
) -> LongTermMemoryListResponse:
    """列出当前用户当前有效的长期记忆。"""

    try:
        memories = await PostgresUserMemoryRepository(
            session
        ).list_active_long_term(
            tenant_id=str(principal.tenant_id),
            user_id=str(principal.user_id),
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:
        _raise_memory_http_error(exc)
    return LongTermMemoryListResponse(memories=tuple(memories))


def _scope_from_principal(principal: DataLoopPrincipal) -> UserMemoryScope:
    return UserMemoryScope(
        tenant_id=str(principal.tenant_id),
        user_id=str(principal.user_id),
        team_id=principal.team_id,
        section_id=principal.section_id,
        role_id=principal.role_id,
    )


def _raise_memory_http_error(exc: Exception) -> None:
    if isinstance(exc, MemoryPromotionTargetNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(
        exc,
        (
            MemoryWriteConflictError,
            MemoryPromotionConflictError,
        ),
    ):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(
        exc,
        (
            MemoryWritePersistenceError,
            MemoryPromotionPersistenceError,
            MemoryContextPersistenceError,
            UserMemoryDataCorruptedError,
        ),
    ):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, (MemoryPromotionRejectedError, ValueError)):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # 未知缺陷保留原始 traceback，交由上层记录为 500。
    raise exc
