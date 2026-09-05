from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from app.config import get_settings
from app.services.hot_news_orchestration import HotNewsRunRequest
from app.workflows.contracts import HotNewsActivityOutcome
from app.workflows.hot_news import (
    HOT_NEWS_WORKFLOW_NAME,
    HotNewsMonitorWorkflow,
)
from examples.hot_news_e2e_support import (
    build_hot_news_e2e_dependencies,
)

async def run() -> None:
    settings = get_settings()
    dependencies = build_hot_news_e2e_dependencies()

    request = HotNewsRunRequest(
        tenant_id=dependencies.tenant_id,
        window_start=dependencies.window_start,
        window_end=dependencies.window_end,
        production_bundle_version=(
            dependencies.production_bundle_version
        ),
        workflow_version=HOT_NEWS_WORKFLOW_NAME,
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