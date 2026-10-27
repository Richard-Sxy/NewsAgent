"""NewsAgent API 薄入口。

只负责路由注册、健康检查与静态控制台挂载；
依赖装配与资源生命周期在 app.bootstrap.api_lifespan。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from fastapi.responses import PlainTextResponse

from app.api import (
    data_loop_router,
    events_router,
    hot_news_router,
    jobs_router,
    memory_router,
    security_tests_router,
    knowledge_router,
    conversations_router,
)
from app.api.audience import router as audience_router
from app.api.behavior_tags import router as behavior_tags_router
from app.api.reader_profiles import router as reader_profiles_router
from app.api.hot_news_monitor import router as hot_news_monitor_router
from app.observability.web import TelemetryMiddleware
from app.bootstrap.api_lifespan import lifespan


def create_app() -> FastAPI:
    application = FastAPI(
        title="News Writing Agent Service",
        version="0.2.0",
        lifespan=lifespan,
    )
    application.add_middleware(TelemetryMiddleware)
    application.include_router(jobs_router)
    application.include_router(events_router)
    application.include_router(data_loop_router)
    application.include_router(hot_news_router)
    application.include_router(memory_router)
    application.include_router(security_tests_router)
    application.include_router(knowledge_router)
    application.include_router(conversations_router)
    application.include_router(audience_router)
    application.include_router(behavior_tags_router)
    application.include_router(reader_profiles_router)
    application.include_router(hot_news_monitor_router)

    application.get("/health")(health)
    application.get("/ready")(ready)
    application.get("/metrics", response_class=PlainTextResponse)(metrics)

    return application


def health() -> dict[str, str]:
    return {"status": "ok"}


async def metrics(request: Request) -> PlainTextResponse:
    """Prometheus text endpoint for business state and outbox reliability."""
    async with request.app.state.database.session() as session:
        jobs = await session.execute(
            text("SELECT status::text, count(*) FROM writing_jobs GROUP BY status")
        )
        outbox = await session.execute(
            text("SELECT status, count(*) FROM outbox_events GROUP BY status")
        )
    lines = [
        "# HELP news_agent_jobs Number of writing jobs by status.",
        "# TYPE news_agent_jobs gauge",
    ]
    lines.extend(
        f'news_agent_jobs{{status="{status}"}} {count}' for status, count in jobs
    )
    lines.extend(
        [
            "# HELP news_agent_outbox_events Number of outbox events by status.",
            "# TYPE news_agent_outbox_events gauge",
        ]
    )
    lines.extend(
        f'news_agent_outbox_events{{status="{status}"}} {count}'
        for status, count in outbox
    )
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


async def ready(request: Request) -> JSONResponse:
    """Readiness checks critical dependencies; liveness stays process-only."""
    checks: dict[str, str] = {}
    try:
        async with request.app.state.database.session() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "error"
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "error"
    try:
        await request.app.state.temporal.service_client.check_health()
        checks["temporal"] = "ok"
    except Exception:
        checks["temporal"] = "error"

    ready_status = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if ready_status else 503,
        content={"status": "ready" if ready_status else "not_ready", "checks": checks},
    )


app = create_app()
