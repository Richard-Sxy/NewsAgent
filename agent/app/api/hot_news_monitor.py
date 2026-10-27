"""Authenticated chat feed; the isolated source is never exposed to the browser."""
import asyncio
import re
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.api.dependencies import DataLoopPrincipal, HotNewsPermission, require_hot_news_permission
from app.modules.hot_news.contracts.monitor import MonitorFeed, MonitorStart, MonitorSwitch

router = APIRouter(prefix="/api/v1/hot-news-monitor", tags=["hot-news-monitor"])
ReadPrincipal = Annotated[DataLoopPrincipal, Depends(require_hot_news_permission(HotNewsPermission.READ))]
AdminPrincipal = Annotated[DataLoopPrincipal, Depends(require_hot_news_permission(HotNewsPermission.ADMIN))]
LOCAL_ENVIRONMENTS = {"local", "development", "test", "e2e"}

def source_url(settings):
    value = getattr(settings, "hot_news_monitor_url", None)
    if not value:
        return None
    parsed = urlsplit(value)
    if (getattr(settings, "environment", None) not in LOCAL_ENVIRONMENTS or
            parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("Monitor source requires an explicit local service origin")
    return value.rstrip("/")

async def source_request(url, tenant_id, *, suffix="", method="GET", body=None):
    # Trusted deployment configuration only; no model/browser-selected destinations.
    async with asyncio.timeout(5), httpx.AsyncClient(timeout=4, trust_env=False, follow_redirects=False) as client:
        async with client.stream(method, url + "/streaming-lab-api/hot-monitor-feed" + suffix,
                params={"tenant_id":tenant_id}, json=body) as response:
            if response.status_code in (404, 409, 422):
                raise HTTPException(response.status_code, "监控源尚未就绪或监控不存在，请检查持续采集状态。")
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 262144:
                    raise ValueError("Monitor response exceeded budget")
    value = MonitorFeed.model_validate_json(raw)
    if value.tenant_id != tenant_id:
        raise ValueError("Monitor source crossed tenant scope")
    monitor_ids = {m.monitor_id for m in value.monitors if m.enabled}
    if any(item.monitor_id not in monitor_ids for item in value.items):
        raise ValueError("Notice has no enabled monitor")
    return value

async def read_feed(request, principal):
    settings = getattr(request.app.state, "settings", None)
    url = source_url(settings)
    tenant_id = str(principal.tenant_id)
    if not url:
        return MonitorFeed(configured=False, tenant_id=tenant_id, source_state="disabled")
    try:
        result = await source_request(url, tenant_id)
    except (httpx.HTTPError, ValueError, HTTPException, TimeoutError):
        return MonitorFeed(tenant_id=tenant_id, source_state="unavailable",
                           can_manage=HotNewsPermission.ADMIN.value in principal.permissions)
    result.can_manage = HotNewsPermission.ADMIN.value in principal.permissions
    return result

@router.get("")
async def snapshot(request: Request, response: Response, principal: ReadPrincipal):
    response.headers["Cache-Control"] = "private, no-store"
    return await read_feed(request, principal)

async def feed_events(request, principal, *, interval=5, max_cycles=12):
    previous = None
    yield "retry: 5000\n\n"
    for _ in range(max_cycles):
        if await request.is_disconnected():
            return
        result = await read_feed(request, principal)
        payload = result.model_dump_json()
        if payload != previous:
            yield "event: hotspots\ndata: " + payload + "\n\n"
            previous = payload
        else:
            yield ": heartbeat\n\n"
        if not result.configured or result.source_state == "unavailable":
            return
        await asyncio.sleep(interval)
    # Renew explicitly so a normal authentication refresh does not look like an outage.
    yield "event: reconnect\ndata: {}\n\n"
    # Bounded connections re-enter gateway authentication on browser reconnect.

@router.get("/events")
async def events(request: Request, principal: ReadPrincipal):
    return StreamingResponse(feed_events(request, principal), media_type="text/event-stream",
        headers={"Cache-Control":"private, no-store","X-Accel-Buffering":"no"})

async def mutate(request, principal, suffix, method, body):
    url = source_url(getattr(request.app.state, "settings", None))
    if not url:
        raise HTTPException(503, "持续监控尚未配置")
    try:
        value = await source_request(url, str(principal.tenant_id), suffix=suffix, method=method, body=body)
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        # No mutation retries: the next read shows whether it was committed.
        raise HTTPException(503, "监控操作结果待确认，请刷新状态后再操作。") from exc
    value.can_manage = True
    return value

@router.post("", response_model=MonitorFeed)
async def start(body: MonitorStart, request: Request, response: Response, principal: AdminPrincipal):
    response.headers["Cache-Control"] = "private, no-store"
    return await mutate(request, principal, "", "POST", body.model_dump())

@router.put("/{monitor_id}", response_model=MonitorFeed)
async def switch(monitor_id: str, body: MonitorSwitch, request: Request, response: Response, principal: AdminPrincipal):
    if not re.fullmatch("[0-9a-f]{64}", monitor_id):
        raise HTTPException(404, "监控不存在")
    response.headers["Cache-Control"] = "private, no-store"
    return await mutate(request, principal, "/" + monitor_id, "PUT", body.model_dump())

