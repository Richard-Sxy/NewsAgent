"""热点运营控制台 API：只读榜单/详情与运营决策入口。

鉴权模型：与 Data Loop 复用同一个网关共享 Bearer Token
（DATA_LOOP_GATEWAY_TOKEN），但权限串来自独立的 ``X-Hot-News-Roles``
头；租户与用户身份只信任网关注入的 ``X-Tenant-ID`` / ``X-User-ID``。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.service import RPCError

from app.api.dependencies import (
    DataLoopPrincipal,
    HotNewsPermission,
    get_hot_news_decision_service,
    get_hot_news_event_stream,
    get_hot_news_query_service,
    get_hot_news_writing_handoff_service,
    get_session,
    require_hot_news_permission,
)
from app.domain.errors import HotNewsPersistenceError
from app.schemas.hot_news_api import (
    HandoffHotNewsToWritingRequest,
    HotNewsRunDetailResponse,
    HotNewsRunListResponse,
    HotNewsWritingHandoffResponse,
    RecordHotNewsDecisionRequest,
    RecordHotNewsDecisionResponse,
)
from app.schemas.hot_news_decision import RecordHotNewsDecisionCommand
from app.schemas.job import WritingJobResponse
from app.services.hot_news_decision import (
    HotNewsDecisionConflictError,
    HotNewsDecisionPersistenceError,
    HotNewsDecisionService,
    HotNewsDecisionTargetNotFoundError,
)
from app.services.hot_news_query import HotNewsQueryService
from app.schemas.hot_news_events import HotNewsProgressEvent
from app.services.hot_news_event_stream import (
    HotNewsProgressReadError,
    InvalidHotNewsStreamEventID,
    RedisHotNewsEventStream,
)
from app.services.hot_news_writing_handoff import (
    HotNewsAnalysisNotFoundError,
    HotNewsWritingHandoffService,
)
from app.services.job import JobNotFoundError


router = APIRouter(prefix="/api/v1/hot-news", tags=["hot-news-console"])

ReadPrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_hot_news_permission(HotNewsPermission.READ)),
]
DecidePrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_hot_news_permission(HotNewsPermission.DECIDE)),
]
HandoffPrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_hot_news_permission(HotNewsPermission.HANDOFF)),
]


def format_hot_news_sse(
    event: HotNewsProgressEvent,
    retry_ms: int,
) -> str:
    data = event.model_dump_json()
    return (
        f"id: {event.event_id}\n"
        f"event: {event.event}\n"
        f"retry: {retry_ms}\n"
        f"data: {data}\n\n"
    )


@router.get("/streams/{run_key}/events")
async def stream_hot_news_events(
    run_key: Annotated[
        str,
        Path(pattern=r"^hot-news-[0-9a-f]{64}$"),
    ],
    request: Request,
    principal: ReadPrincipal,
    last_event_id: str | None = Header(
        default=None,
        alias="Last-Event-ID",
    ),
    stream: RedisHotNewsEventStream = Depends(
        get_hot_news_event_stream
    ),
) -> StreamingResponse:
    """按热点运行幂等键推送短期进度；完整结果仍从 PostgreSQL API 获取。"""

    try:
        cursor = stream.validate_event_id(last_event_id)
    except InvalidHotNewsStreamEventID as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def generate() -> AsyncIterator[str]:
        current_id = cursor
        while not await request.is_disconnected():
            try:
                events = await stream.read(
                    tenant_id=str(principal.tenant_id),
                    run_key=run_key,
                    last_event_id=current_id,
                )
            except asyncio.CancelledError:
                break
            except HotNewsProgressReadError:
                yield ": hot-news progress stream temporarily unavailable\n\n"
                await asyncio.sleep(1)
                continue
            if not events:
                yield ": heartbeat\n\n"
                continue
            for event in events:
                if event.event_id is None:
                    continue
                current_id = event.event_id
                yield format_hot_news_sse(
                    event,
                    request.app.state.settings.sse_retry_ms,
                )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs", response_model=HotNewsRunListResponse)
async def list_hot_news_runs(
    principal: ReadPrincipal,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    query_service: HotNewsQueryService = Depends(get_hot_news_query_service),
) -> HotNewsRunListResponse:
    """列出当前租户的热点运行（按窗口倒序），供控制台选择榜单窗口。"""

    try:
        runs = await query_service.list_runs(
            tenant_id=str(principal.tenant_id),
            offset=offset,
            limit=limit,
        )
    except HotNewsPersistenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return HotNewsRunListResponse(runs=runs, offset=offset, limit=limit)


@router.get("/runs/{run_id}", response_model=HotNewsRunDetailResponse)
async def get_hot_news_run(
    run_id: UUID,
    principal: ReadPrincipal,
    query_service: HotNewsQueryService = Depends(get_hot_news_query_service),
) -> HotNewsRunDetailResponse:
    """单次运行的榜单、热度分量、分析摘要与决策记录；租户隔离。"""

    try:
        detail = await query_service.get_run_detail(
            tenant_id=str(principal.tenant_id),
            run_id=run_id,
        )
    except HotNewsPersistenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="hot news run not found",
        )
    return detail


@router.post(
    "/decisions",
    response_model=RecordHotNewsDecisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_hot_news_decision(
    request: RecordHotNewsDecisionRequest,
    principal: DecidePrincipal,
    service: HotNewsDecisionService = Depends(get_hot_news_decision_service),
) -> RecordHotNewsDecisionResponse:
    """记录运营决策；同一 idempotency_key 重放返回原决策，不产生重复反馈。"""

    try:
        decision, created = await service.record_decision(
            tenant_id=str(principal.tenant_id),
            operator_id=str(principal.user_id),
            command=RecordHotNewsDecisionCommand(
                run_id=request.run_id,
                news_id=request.news_id,
                decision_type=request.decision_type,
                reason=request.reason,
                correction_payload=request.correction_payload,
                idempotency_key=request.idempotency_key,
                supersedes_decision_id=request.supersedes_decision_id,
            ),
            feedback_problem_type=request.feedback_problem_type,
            feedback_severity=request.feedback_severity,
        )
    except HotNewsDecisionTargetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HotNewsDecisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HotNewsDecisionPersistenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RecordHotNewsDecisionResponse(
        decision_id=decision.id,
        decision_type=decision.decision_type,
        status="recorded",
        created=created,
    )


@router.post(
    "/runs/{run_id}/news/{news_id}/handoff",
    response_model=HotNewsWritingHandoffResponse,
    status_code=status.HTTP_201_CREATED,
)
async def handoff_hot_news_to_writing(
    run_id: UUID,
    news_id: str,
    request: HandoffHotNewsToWritingRequest,
    principal: HandoffPrincipal,
    session: AsyncSession = Depends(get_session),
    service: HotNewsWritingHandoffService = Depends(
        get_hot_news_writing_handoff_service
    ),
) -> HotNewsWritingHandoffResponse:
    """把一条已校验热点分析转交研究/写作流程；同场景重复转交幂等。"""

    try:
        outcome = await service.handoff(
            session,
            tenant_id=principal.tenant_id,
            created_by=principal.user_id,
            run_id=run_id,
            news_id=news_id,
            scenario=request.scenario,
        )
    except HotNewsAnalysisNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RPCError as exc:
        raise HTTPException(
            status_code=503,
            detail="Temporal 暂时不可用，请稍后重试",
        ) from exc
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return HotNewsWritingHandoffResponse(
        job=WritingJobResponse.model_validate(outcome.job),
        created=outcome.created,
    )
