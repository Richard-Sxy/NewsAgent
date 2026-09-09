"""Temporal Worker dedicated to bounded Data Loop workflows."""

from __future__ import annotations

from temporalio.client import Client
from temporalio.worker import Worker

from app.activities.data_loop import DataLoopActivities, DataLoopStepHandler
from app.config import Settings, get_settings
from app.workflows.data_loop import HotNewsDataLoopWorkflow


async def run_data_loop_worker(
    handler: DataLoopStepHandler,
    *,
    settings: Settings | None = None,
) -> None:
    """Register one Workflow and one thin Activity on a dedicated queue."""

    resolved = settings or get_settings()
    client = await Client.connect(
        resolved.temporal_address,
        namespace=resolved.temporal_namespace,
    )
    activities = DataLoopActivities(handler)
    worker = Worker(
        client,
        task_queue=resolved.temporal_data_loop_task_queue,
        workflows=[HotNewsDataLoopWorkflow],
        activities=[activities.run_data_loop_step],
    )
    await worker.run()


def main() -> None:
    raise SystemExit(
        "The Data Loop worker requires an application-specific step handler. "
        "Call run_data_loop_worker(handler=...) from the deployment bootstrap."
    )


if __name__ == "__main__":
    main()
