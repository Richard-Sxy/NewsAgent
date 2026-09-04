from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from app.config import Settings
from app.models.job import WritingJob
from app.workflows.contracts import HumanDecision, NewsWritingInput, WorkflowSnapshot
from app.workflows.news_writing import NewsWritingWorkflow


class InvalidHumanDecisionError(ValueError):
    pass


class OrchestratorService:
    """Writing API 与 Temporal 的唯一交互边界。"""

    def __init__(self, client: Client, settings: Settings) -> None:
        self.client = client
        self.task_queue = settings.temporal_task_queue

    """ 启动工作流 """
    async def start_job(
        self,
        job: WritingJob,
        *,
        recovery_action: str | None = None,
        recovery_instruction: str | None = None,
    ) -> None:
        await self.client.start_workflow(
            NewsWritingWorkflow.run,
            NewsWritingInput(
                tenant_id=str(job.tenant_id),
                job_id=str(job.id),
                topic=job.topic,
                requirements=job.requirements,
                scenario=job.scenario.value,
                recovery_action=recovery_action,
                recovery_instruction=recovery_instruction,
            ),
            id=job.temporal_workflow_id,
            task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )

    """ 获取进程 """
    async def get_progress(self, workflow_id: str) -> WorkflowSnapshot:
        handle = self.client.get_workflow_handle(workflow_id)
        return await handle.query(
            NewsWritingWorkflow.snapshot,
            result_type=WorkflowSnapshot,
        )
    
    """ 提交人工决定 """
    async def submit_human_decision(
        self,
        workflow_id: str,
        decision: HumanDecision,
    ) -> None:
        handle = self.client.get_workflow_handle(workflow_id)
        # 先查询 Temporal Workflow 某一时刻的当前状态图，在真正发送人工校验前先做决策。
        snapshot = await handle.query(
            NewsWritingWorkflow.snapshot,
            result_type=WorkflowSnapshot,
        )
        if snapshot.waiting_gate != decision.gate:
            raise InvalidHumanDecisionError(
                f"当前等待 {snapshot.waiting_gate!r}，不能提交 {decision.gate!r} 决策"
            )
        await handle.signal(NewsWritingWorkflow.submit_human_decision, decision)
