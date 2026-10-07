"""聊天接口。创建和查询会话、发送消息、查询聊天运行能力，以及通过 SSE 返回处理进度和答复。调用 ConversationAgentService """

import asyncio
from typing import Annotated
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DataLoopPrincipal, HotNewsPermission, require_hot_news_permission
from app.conversation.streaming import StreamingConversation
from app.repositories.conversation import ConversationConflict, ConversationNotFound
from app.schemas.conversation import (ConversationDetailResponse, ConversationListResponse,
                                      ConversationTurnView, ConversationView, CreateConversationRequest,
                                      SendConversationMessageRequest)


router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])
Principal = Annotated[DataLoopPrincipal, Depends(require_hot_news_permission(HotNewsPermission.READ))]


class ConversationStreamingResponse(StreamingResponse):
    """Close the iterator explicitly even when an ASGI send raises on disconnect."""

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


def service(request: Request):
    value = getattr(request.app.state, "conversation_service", None)
    if value is None:
        raise HTTPException(status_code=503, detail="conversation Agent is not enabled")
    return value


@router.get("/runtime")
async def conversation_runtime(request: Request, principal: Principal):
    return service(request).runtime_info(tenant_id=str(principal.tenant_id),
        hot_news_query_allowed=HotNewsPermission.ADMIN.value in principal.permissions)


@router.get("", response_model=ConversationListResponse)
async def list_conversations(request: Request, principal: Principal, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    try:
        items = await service(request).repository.list(tenant_id=str(principal.tenant_id), user_id=str(principal.user_id), limit=limit)
        return ConversationListResponse(items=items)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="conversation storage unavailable") from exc


@router.post("", response_model=ConversationView, status_code=201)
async def create_conversation(body: CreateConversationRequest, request: Request, principal: Principal):
    try:
        return await service(request).repository.create(tenant_id=str(principal.tenant_id), user_id=str(principal.user_id), title=body.title)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="conversation storage unavailable") from exc


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: UUID, request: Request, principal: Principal):
    try:
        current = service(request)
        await current.repository.delete(
            tenant_id=str(principal.tenant_id), user_id=str(principal.user_id),
            conversation_id=conversation_id,
            stale_after_seconds=int(current.turn_timeout_seconds) + 30,
        )
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except ConversationConflict as exc:
        raise HTTPException(status_code=409, detail="conversation has a processing turn") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="conversation storage unavailable") from exc
    return Response(status_code=204)


@router.post("/{conversation_id}/restore", response_model=ConversationView)
async def restore_conversation(conversation_id: UUID, request: Request, principal: Principal):
    try:
        return await service(request).repository.restore(
            tenant_id=str(principal.tenant_id), user_id=str(principal.user_id),
            conversation_id=conversation_id,
        )
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="conversation storage unavailable") from exc


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def read_conversation(conversation_id: UUID, request: Request, principal: Principal,
                            limit: Annotated[int, Query(ge=1, le=100)] = 50):
    try:
        current = service(request)
        result = await current.repository.get(
            tenant_id=str(principal.tenant_id), user_id=str(principal.user_id),
            conversation_id=conversation_id, limit=limit,
            stale_after_seconds=int(current.turn_timeout_seconds) + 30,
        )
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="conversation storage unavailable") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return result


@router.post("/{conversation_id}/messages", response_model=ConversationTurnView)
async def send_message(conversation_id: UUID, body: SendConversationMessageRequest, request: Request, principal: Principal):
    try:
        return await service(request).send(tenant_id=str(principal.tenant_id), user_id=str(principal.user_id),
                                           conversation_id=conversation_id, request_id=body.request_id, content=body.content,
                                           hot_news_query_allowed=HotNewsPermission.ADMIN.value in principal.permissions)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except ConversationConflict as exc:
        raise HTTPException(status_code=409, detail="message is processing, expired, or conflicts with an existing request") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="conversation storage unavailable") from exc


@router.post("/{conversation_id}/messages/stream", response_class=ConversationStreamingResponse)
async def stream_message(conversation_id: UUID, body: SendConversationMessageRequest,
                         request: Request, principal: Principal):
    current = service(request)
    try:
        # 这边设置调用
        async with asyncio.timeout(10):
            claim = await current.prepare_turn(
                tenant_id=str(principal.tenant_id), user_id=str(principal.user_id),
                conversation_id=conversation_id, request_id=body.request_id, content=body.content,
            )
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except ConversationConflict as exc:
        raise HTTPException(status_code=409, detail="message is processing, expired, or conflicts with an existing request") from exc
    except (SQLAlchemyError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="conversation storage unavailable") from exc
    settings = getattr(request.app.state, "settings", None)
    stream = StreamingConversation(
        service=current, claim=claim, tenant_id=str(principal.tenant_id), user_id=str(principal.user_id),
        conversation_id=conversation_id,
        heartbeat_seconds=getattr(settings, "conversation_stream_heartbeat_seconds", 10),
        chunk_chars=getattr(settings, "conversation_stream_chunk_chars", 128),
        hot_news_query_allowed=HotNewsPermission.ADMIN.value in principal.permissions,
    )
    return ConversationStreamingResponse(stream.iter_events(), media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})
