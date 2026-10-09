"""Local-only HTTP trigger for the existing hot-news E2E workflow.

The production ASGI app never imports or mounts this router.  The isolated
Compose stack opts into it explicitly and binds the API to loopback.
"""

from __future__ import annotations

import asyncio
import json
import os
from hashlib import sha256
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from temporalio.client import WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from app.api.dependencies import (
    DataLoopPrincipal,
    HotNewsPermission,
    require_hot_news_permission,
)
from app.main import create_app
from app.repositories.production_bundle import (
    PostgresProductionBundleRepository,
    ProductionBundleDataCorruptedError,
)
from app.schemas.production_bundle import ProductionBundleSpec
from app.services.hot_news_orchestration import HotNewsRunRequest
from app.services.production_bundle import (
    ProductionBundleApplicationService,
    ProductionBundleConflictError,
    ProductionBundlePersistenceError,
)
from app.model_runtime.bundle_runtime import NativeProductionBundleRuntimeRegistry
from app.model_runtime.bundle_contract import UnsupportedProductionBundleRuntimeError
from app.model_runtime.config_file import load_model_runtime_config
from app.workflows.contracts import HotNewsActivityOutcome
from app.workflows.hot_news import HOT_NEWS_WORKFLOW_NAME, HotNewsMonitorWorkflow
from examples.hot_news_e2e_support import E2E_TENANT_ID
from app.api.sql_assistant import api_error, service as sql_service
from app.schemas.sql_assistant import SqlAssistantIntent, SqlAssistantPreviewRequest
from app.conversation.query_understanding import QueryResolutionError
from app.sql_assistant.bootstrap import build_local_hot_news_sql_service
from app.sql_assistant.hot_news_binding import build_hot_news_sql_scope
from app.sql_assistant.scenarios import load_sql_scenarios
from app.sql_assistant.service import SqlAssistantError
from app.sql_assistant.warehouse import WINDOW_START, WINDOW_END, dataset_window, STREAMED_PROFILES


LOCAL_BUNDLE_VERSION = "local-simulation-bundle-v1"
router = APIRouter(prefix="/api/v1/local-simulation", tags=["local-simulation"])
AdminPrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_hot_news_permission(HotNewsPermission.ADMIN)),
]
ReadPrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_hot_news_permission(HotNewsPermission.READ)),
]


class LocalHotNewsRunResponse(BaseModel):
    workflow_id: str
    run_id: UUID
    status: Literal["completed"]
    production_bundle_version: str
    ranked_news_count: int
    analyzed_news_count: int
    sql_query_id: str


def create_local_app() -> FastAPI:
    """Fail closed unless the explicitly isolated E2E deployment opted in."""

    if (
        os.environ.get("ENVIRONMENT") != "e2e"
        or os.environ.get("LOCAL_SIMULATION_ENABLED") != "1"
    ):
        raise RuntimeError("local simulation is only available in the E2E stack")
    app = create_app()
    app.include_router(router)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def local_lifespan(application: FastAPI):
        async with original_lifespan(application):
            settings = application.state.settings
            application.state.sql_assistant = await build_local_hot_news_sql_service(
                database=application.state.database, settings=settings,
                model_config=application.state.model_config,
                model_ports=application.state.model_ports,
            )
            if settings.conversation_enabled and settings.conversation_hot_news_query_enabled:
                from examples.conversation_hot_news import LocalConversationHotNewsQuery
                application.state.conversation_service.enable_hot_news_query(
                    LocalConversationHotNewsQuery(application.state),
                )
            yield

    app.router.lifespan_context = local_lifespan
    return app


@router.get("/hot-news/sql-config")
async def hot_news_sql_config(request: Request, principal: ReadPrincipal) -> dict:
    if str(principal.tenant_id) != E2E_TENANT_ID:
        raise HTTPException(status_code=403, detail="local scenario tenant required")
    try:
        return sql_service(request).config()
    except (ValueError, OSError, RuntimeError) as exc:
        raise api_error(exc) from exc


@router.post("/hot-news/run", response_model=LocalHotNewsRunResponse)
async def run_local_hot_news(
    request: Request,
    principal: AdminPrincipal,
    body: SqlAssistantPreviewRequest | None = None,
) -> LocalHotNewsRunResponse:
    """Plan a bounded SQL tool, bind it, then run the real hot-news Workflow."""

    return await execute_local_hot_news_query(state=request.app.state, principal=principal, body=body)

"""Sql请求/校验原则/Sql意图->输出请求"""
async def execute_local_hot_news_query(*, state, principal: DataLoopPrincipal,
                                     body: SqlAssistantPreviewRequest | None = None,
                                     on_bound=None,
                                     expected_intent: SqlAssistantIntent | None = None) -> LocalHotNewsRunResponse:
    """Shared command for the workbench and conversation; no internal HTTP call."""

    if HotNewsPermission.ADMIN.value not in principal.permissions:
        raise HTTPException(status_code=403, detail="local query requires hot-news:admin")

    if str(principal.tenant_id) != E2E_TENANT_ID:
        raise HTTPException(status_code=403, detail="local scenario tenant required")

    sample_start, sample_end = dataset_window(state.settings.sql_assistant_dataset_profile)
    body = body or SqlAssistantPreviewRequest(
        question="按点击量取前5条新闻", scenario_id="news-ranking",
        window_start=sample_start, window_end=sample_start + timedelta(hours=1),
    )
    if body.window_end - body.window_start != timedelta(hours=1):
        raise HTTPException(status_code=422, detail="热点本地模拟请选择恰好1小时，小时去重用户数不能跨小时相加")
    if body.window_start < sample_start or body.window_end > sample_end:
        raise HTTPException(status_code=422, detail="窗口必须位于当前数据集公布的合成样本范围内")

    settings = state.settings
    try:
        manifest = json.loads(settings.hot_news_runtime_manifest_json)
        if not isinstance(manifest, list) or not manifest:
            raise ValueError("empty runtime manifest")
        first_spec = ProductionBundleSpec.model_validate(manifest[0])
        model_config = load_model_runtime_config(settings.model_runtime_config_path)
        registry = NativeProductionBundleRuntimeRegistry.validation_only(
            model_config.prompt_registry(),
            settings.hot_news_runtime_manifest_json,
            allowed_model_routes=model_config.inference.model_routes,
        )
    except (TypeError, ValueError, UnsupportedProductionBundleRuntimeError) as exc:
        raise HTTPException(status_code=503, detail="local runtime manifest unavailable") from exc

    try:
        async with state.database.session() as session:
            repository = PostgresProductionBundleRepository(session)
            active = await repository.get_active_bundle(tenant_id=E2E_TENANT_ID)
            if active is None:
                outcome = await ProductionBundleApplicationService().bootstrap_initial_bundle(
                    session,
                    tenant_id=E2E_TENANT_ID,
                    bundle_version=LOCAL_BUNDLE_VERSION,
                    spec=first_spec,
                    actor_id=str(principal.user_id),
                    now=datetime.now(timezone.utc),
                )
                active = outcome.bundle
    except (ProductionBundleConflictError, ProductionBundleDataCorruptedError) as exc:
        raise HTTPException(status_code=409, detail="local bundle is conflicting") from exc
    except (ProductionBundlePersistenceError, SQLAlchemyError) as exc:
        raise HTTPException(status_code=503, detail="local bundle store unavailable") from exc

    try:
        registry.ensure_supported(active.spec)
    except UnsupportedProductionBundleRuntimeError as exc:
        raise HTTPException(status_code=409, detail="active local bundle is unsupported") from exc

    try:
        tool = state.sql_assistant
        config = load_sql_scenarios(settings.sql_assistant_scenarios_path)
        try:
            scene = config.resolve(body.scenario_id)
        except ValueError as exc:
            raise SqlAssistantError("查询场景不存在，请重新读取配置", 422) from exc
        if scene.result_mode != "ranking":
            raise SqlAssistantError("热点 Agent 只接受新闻候选排名场景，不接受跨新闻趋势聚合", 422)
        model_binding = model_config.agent_scene(config.model_scene)
        scope = build_hot_news_sql_scope(
            question=body.question.strip(), scenario_id=body.scenario_id,
            window_start=body.window_start, window_end=body.window_end,
            schema_sha256=tool.contract.sha256, production_bundle_version=active.bundle_version,
            warehouse_schema_version=tool.contract.version,
            dataset_identity=tool.dataset_identity() if tool.dataset_profile in STREAMED_PROFILES else None,
            model_scene=config.model_scene,
            user_id=str(principal.user_id),
            scenario={"scene": scene.model_dump(mode="json"),
                      "model_binding": model_binding.model_dump(mode="json"),
                      "model_provider": model_config.inference.provider,
                      "prompt_sha256": sha256(next(
                          prompt.system_prompt for prompt in model_config.prompts
                          if prompt.scene == config.model_scene and prompt.version == model_binding.prompt_version
                      ).encode()).hexdigest(),
                      "query_timeout_ms": config.query_timeout_ms},
        )
        preview_kwargs = {"tenant_id": E2E_TENANT_ID, "user_id": str(principal.user_id)}
        if expected_intent is not None:
            preview_kwargs["expected_intent"] = expected_intent
        preview = await tool.preview(body, **preview_kwargs)
        run_request = HotNewsRunRequest(
            tenant_id=E2E_TENANT_ID, window_start=body.window_start, window_end=body.window_end,
            production_bundle_version=active.bundle_version, workflow_version=HOT_NEWS_WORKFLOW_NAME,
            sql_query_id=preview.query_id, sql_query_user_id=str(principal.user_id), sql_query_scope_sha256=scope,
        )
        run_request.validate()
        binding = await tool.hot_news_bindings.get_or_claim(
            run_key=run_request.idempotency_key, query_id=preview.query_id,
            tenant_id=E2E_TENANT_ID, user_id=str(principal.user_id),
            production_bundle_version=active.bundle_version, scope_sha256=scope,
            window_start=body.window_start, window_end=body.window_end,
        )
        from dataclasses import replace
        run_request = replace(run_request, sql_query_id=str(binding["query_id"]))
    except QueryResolutionError:
        raise
    except (ValueError, RuntimeError, SQLAlchemyError, OSError) as exc:
        raise api_error(exc) from exc

    if on_bound is not None:
        await on_bound({
            "workflow_id": run_request.idempotency_key, "sql_query_id": run_request.sql_query_id,
            "production_bundle_version": active.bundle_version,
            "window_start": body.window_start.isoformat(), "window_end": body.window_end.isoformat(),
            "question": body.question, "scenario_id": body.scenario_id,
        })

    try:
        async with asyncio.timeout(120):
            try:
                handle = await state.temporal.start_workflow(
                    HotNewsMonitorWorkflow.run,
                    run_request,
                    id=run_request.idempotency_key,
                    task_queue=settings.temporal_hot_news_task_queue,
                    result_type=HotNewsActivityOutcome,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                )
            except WorkflowAlreadyStartedError as exc:
                handle = state.temporal.get_workflow_handle(
                    run_request.idempotency_key,
                    run_id=exc.run_id,
                )
            result = await handle.result()
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="local workflow timed out") from exc
    except RPCError as exc:
        raise HTTPException(status_code=503, detail="Temporal is unavailable") from exc
    except WorkflowFailureError as exc:
        raise HTTPException(status_code=502, detail="local workflow failed") from exc

    if isinstance(result, dict):
        result = HotNewsActivityOutcome(**result)
    if not isinstance(result, HotNewsActivityOutcome) or result.status != "completed":
        raise HTTPException(status_code=502, detail="local workflow did not complete")
    return LocalHotNewsRunResponse(
        workflow_id=run_request.idempotency_key,
        run_id=UUID(result.run_id),
        status="completed",
        production_bundle_version=active.bundle_version,
        ranked_news_count=result.ranked_news_count,
        analyzed_news_count=result.analyzed_news_count,
        sql_query_id=run_request.sql_query_id,
    )
