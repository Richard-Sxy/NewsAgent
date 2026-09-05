"""热点监控 Temporal Workflow。"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.services.hot_news_orchestration import HotNewsRunRequest
    from app.workflows.contracts import HotNewsActivityOutcome


HOT_NEWS_WORKFLOW_NAME = "hot-news-workflow-v1"

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
