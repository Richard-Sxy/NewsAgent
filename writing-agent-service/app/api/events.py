import asyncio
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    get_event_reader,
    get_job_service,
    get_session,
    get_tenant_id,
)
from app.schemas.events import ProgressEvent
from app.services.event_reader import (
    InvalidStreamEventID,
    ProgressReadError,
    RedisProgressReader,
)
from app.services.job import JobNotFoundError, JobService

router = APIRouter(prefix="/api/v1/jobs", tags=["writing-events"])


def format_sse(event: ProgressEvent, retry_ms: int) -> str:
    data = event.model_dump_json()
    return (
        f"id: {event.event_id}\n"
        f"event: {event.event}\n"
        f"retry: {retry_ms}\n"
        f"data: {data}\n\n"
    )


@router.get("/{job_id}/events")
async def stream_job_events(
    job_id: uuid.UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    jobs: JobService = Depends(get_job_service),
    reader: RedisProgressReader = Depends(get_event_reader),
) -> StreamingResponse:
    try:
        await jobs.get(session, tenant_id=tenant_id, job_id=job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        cursor = reader.validate_event_id(last_event_id)
    except InvalidStreamEventID as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def generate() -> AsyncIterator[str]:
        current_id = cursor
        while not await request.is_disconnected():
            try:
                events = await reader.read(tenant_id, job_id, current_id)
            except asyncio.CancelledError:
                break
            except ProgressReadError:
                # Redis 短暂故障时保持连接，浏览器无需丢失当前 cursor。
                yield ": progress stream temporarily unavailable\n\n"
                await asyncio.sleep(1)
                continue
            if not events:
                yield ": heartbeat\n\n"
                continue
            for event in events:
                if event.event_id is None:
                    continue
                current_id = event.event_id
                yield format_sse(event, request.app.state.settings.sse_retry_ms)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
