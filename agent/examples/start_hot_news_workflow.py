from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from app.config import get_settings
from app.modules.behavior_tags.runtime import build_behavior_runtime
from app.modules.reader_profiles.factory import build_reader_profile_service
from app.modules.hot_news.application.hot_news_orchestration import HotNewsRunRequest
from app.workflows.contracts import HotNewsActivityOutcome
from app.modules.hot_news.workflows.hot_news import (
    HOT_NEWS_WORKFLOW_NAME,
    HotNewsMonitorWorkflow,
)
from simulation.hot_news_e2e_support import (
    build_hot_news_e2e_dependencies,
)

async def run() -> None:
    settings = get_settings()
    dependencies = build_hot_news_e2e_dependencies()
    behavior_runtime = build_behavior_runtime(settings)
    behavior_context = behavior_runtime.context_for_window(
        tenant_id=dependencies.tenant_id, window_start=dependencies.window_start,
        window_end=dependencies.window_end,
    ) if behavior_runtime is not None else None
    reader_service = build_reader_profile_service(settings)
    try:
        reader_context = reader_service.context_identity_for_window(
            tenant_id=dependencies.tenant_id, window_start=dependencies.window_start, window_end=dependencies.window_end,
        ) if reader_service is not None else None
    finally:
        if reader_service is not None:
            await reader_service.close()

    request = HotNewsRunRequest(
        tenant_id=dependencies.tenant_id,
        window_start=dependencies.window_start,
        window_end=dependencies.window_end,
        production_bundle_version=(
            dependencies.production_bundle_version
        ),
        workflow_version=HOT_NEWS_WORKFLOW_NAME,
        retrieval_context_sha256=behavior_context.identity_sha256 if behavior_context is not None else None,
        reader_profile_context_sha256=reader_context,
    )
    request.validate()

    print(
        json.dumps(
            {
                "event": "starting_hot_news_workflow",
                "workflow_id": request.idempotency_key,
                "tenant_id": request.tenant_id,
                "window_start": request.window_start.isoformat(),
                "window_end": request.window_end.isoformat(),
                "production_bundle_version": (
                    request.production_bundle_version
                ),
                "task_queue": settings.temporal_hot_news_task_queue,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
    )

    result: HotNewsActivityOutcome = await client.execute_workflow(
        HotNewsMonitorWorkflow.run,
        request,
        id=request.idempotency_key,
        task_queue=settings.temporal_hot_news_task_queue,
        result_type=HotNewsActivityOutcome,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )

    print(
        json.dumps(
            {
                "event": "hot_news_workflow_completed",
                **asdict(result),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
