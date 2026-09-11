"""热点监控 Temporal Workflow。"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.services.hot_news_orchestration import HotNewsRunRequest
    from app.workflows.contracts import (
        HotNewsActivityOutcome,
        HotNewsWindowDispatchRequest,
    )


HOT_NEWS_WORKFLOW_NAME = "hot-news-workflow-v1"
HOT_NEWS_DISPATCH_WORKFLOW_NAME = "hot-news-window-dispatcher-v1"

HOT_NEWS_ACTIVITY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2,
    maximum_interval=timedelta(seconds=30),
    # 第一次执行失败后最多自动重试一次，控制 FastGPT 重复调用成本。
    maximum_attempts=2,
)


@workflow.defn(name=HOT_NEWS_WORKFLOW_NAME)
class HotNewsMonitorWorkflow:
    """编排单个有边界的热点分析窗口。"""

    @workflow.run
    async def run(
        self,
        request: HotNewsRunRequest,
    ) -> HotNewsActivityOutcome:
        return await workflow.execute_activity(
            "run_hot_news_window",
            request,
            start_to_close_timeout=timedelta(minutes=45),
            schedule_to_close_timeout=timedelta(minutes=90),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=HOT_NEWS_ACTIVITY_RETRY_POLICY,
            result_type=HotNewsActivityOutcome,
        )


@workflow.defn(name=HOT_NEWS_DISPATCH_WORKFLOW_NAME)
class HotNewsWindowDispatcherWorkflow:
    """由 Temporal Schedule 周期触发，计算当前窗口并启动热点监控子 Workflow。

    每个租户组一个 Schedule；子 Workflow ID 由租户组与窗口结束时间确定，
    同一窗口的重放不会产生第二次运行（配合分析运行的幂等键）。
    """

    @workflow.run
    async def run(
        self,
        request: HotNewsWindowDispatchRequest,
    ) -> HotNewsActivityOutcome:
        window_end = workflow.now().replace(second=0, microsecond=0)
        window_start = window_end - timedelta(minutes=request.window_minutes)
        run_request = HotNewsRunRequest(
            tenant_id=request.tenant_id,
            window_start=window_start,
            window_end=window_end,
            production_bundle_version=request.production_bundle_version,
            workflow_version=request.workflow_version,
        )
        return await workflow.execute_child_workflow(
            HotNewsMonitorWorkflow.run,
            run_request,
            id=(
                f"hot-news-monitor-{request.tenant_group}"
                f"-{window_end.isoformat()}"
            ),
            result_type=HotNewsActivityOutcome,
        )
