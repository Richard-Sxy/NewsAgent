from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from fastapi.responses import PlainTextResponse
from fastapi.responses import FileResponse
from pathlib import Path
from temporalio.client import Client
from redis.asyncio import Redis

from app.api import (
    data_loop_router,
    events_router,
    hot_news_router,
    jobs_router,
    memory_router,
)
from app.config import get_settings
from app.db.session import Database
from app.services.orchestrator import OrchestratorService
from app.services.event_reader import RedisProgressReader
from app.storage.s3 import S3ArtifactStore
from app.clients.cms import CmsPublisher
from app.services.data_loop.orchestrator import DataLoopOrchestrator
from app.services.data_loop.dataset_freezer import (
    S3EvaluationDatasetArtifactStore,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    database = Database(settings)
    temporal = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
    )
    redis = Redis.from_url(str(settings.redis_url), decode_responses=False)
    app.state.settings = settings
    app.state.database = database
    app.state.orchestrator = OrchestratorService(temporal, settings)
    app.state.data_loop_orchestrator = DataLoopOrchestrator(temporal, settings)
    app.state.temporal = temporal
    app.state.redis = redis
    app.state.event_reader = RedisProgressReader(redis, settings)
    app.state.artifact_store = S3ArtifactStore(settings)
    app.state.evaluation_dataset_artifact_store = (
        S3EvaluationDatasetArtifactStore(settings)
    )
    app.state.cms_publisher = CmsPublisher(settings)
    try:
        yield
    finally:
        await redis.aclose()
        await database.close()


def create_app() -> FastAPI:
    application = FastAPI(
        title="News Writing Agent Service",
        version="0.2.0",
        lifespan=lifespan,
    )
    application.include_router(jobs_router)
    application.include_router(events_router)
    application.include_router(data_loop_router)
    application.include_router(hot_news_router)
    application.include_router(memory_router)

    application.get("/health")(health)
    application.get("/ready")(ready)
    application.get("/metrics", response_class=PlainTextResponse)(metrics)
    application.get("/review", include_in_schema=False)(review_console)
    application.get("/research-package", include_in_schema=False)(research_package_console)
    application.get("/console", include_in_schema=False)(operator_console)

    return application


def health() -> dict[str, str]:
    return {"status": "ok"}


def review_console() -> FileResponse:
    """Minimal same-origin operator console for high-value approval gates."""
    return FileResponse(Path(__file__).parent / "web" / "review.html")


def research_package_console() -> FileResponse:
    """面向运营的资料包可视化工作台。"""
    return FileResponse(Path(__file__).parent / "web" / "research-package.html")


def operator_console() -> FileResponse:
    """热点、写作与 Data Loop 三合一运营总控制台。"""
    return FileResponse(Path(__file__).parent / "web" / "console.html")


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
