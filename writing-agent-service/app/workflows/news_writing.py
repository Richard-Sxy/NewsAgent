from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.contracts import (
        HumanDecision,
        JobStateCommand,
        NewsWritingInput,
        StepCommand,
        StepOutcome,
        WorkflowResult,
        WorkflowSnapshot,
    )


ACTIVITY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


@workflow.defn(name="news-writing-v1")
class NewsWritingWorkflow:
    """显式编排研究、逐节写作、审核和定点返工的长链路。"""

    def __init__(self) -> None:
        self._phase = "created"
        self._completed_steps = 0         # 这边定义类的静态变量
        self._review_round = 0
        self._waiting_gate: str | None = None
        self._human_decision: HumanDecision | None = None
        self._last_artifact_uri: str | None = None
        self._cancelled = False

    # 决定下一步要执行什么
    @workflow.run
    async def run(self, request: NewsWritingInput) -> WorkflowResult:
        try:
            return await self._run_pipeline(request)
        except Exception as exc:
            self._phase = "failed"
            await self._transition_state(
                request,
                "failed",
                self._failure_reason(exc),
            )
            raise

    """"""
    async def _run_pipeline(self, request: NewsWritingInput) -> WorkflowResult:
        research_recovery = request.recovery_action == "research"
        if research_recovery:
            # A research recovery may be needed because the previous Artifact
            # is stale or fails current quality checks. Skip that checkpoint
            # and generate fresh research instead of validating it again.
            research = await self._execute(
                request,
                step_type="research",
                step_key="research_package_v2",
                attempt=2,
                inputs={
                    "topic": request.topic,
                    "requirements": request.requirements,
                    "instruction": request.recovery_instruction,
                },
            )
        else:
            research = await self._execute(
                request,
                step_type="research",
                step_key="research_package_v1",
                inputs={"topic": request.topic, "requirements": request.requirements},
            )
        decision = await self._wait_for_human("research")   # 取消执行
        if decision.action == "cancel":
            return await self._cancelled_result(request)
        if decision.action in {"revise", "research"} and not research_recovery:
            research = await self._execute(
                request,
                step_type="research",
                step_key="research_package_v2",
                attempt=2,
                inputs={
                    "topic": request.topic,
                    "requirements": request.requirements,
                    "previous_artifact": self._artifact_ref(research),
                    "instruction": decision.instruction,
                },
            )
            decision = await self._wait_for_human("research")
            if decision.action == "cancel":
                return await self._cancelled_result(request)
            if decision.action != "approve":
                return await self._waiting_result(request, "研究返工已达到上限")
        if request.scenario == "research_package":
            await self._transition_state(request, "research_completed")
            self._phase = "research_completed"
            return WorkflowResult(
                job_id=request.job_id,
                status="research_completed",
                research_artifact_uri=research.artifact_uri,
            )
        # 执行主题
        outline = await self._execute(
            request,
            step_type="outline",
            step_key="outline_research_v2" if research_recovery else "outline_v1",
            attempt=2 if research_recovery else 1,
            inputs={
                "research_artifact": self._artifact_ref(research),
                "requirements": request.requirements,
            },
        )
        decision = await self._wait_for_human("outline")
        if decision.action == "cancel":
            return await self._cancelled_result(request)
        if decision.action == "revise":
            outline = await self._execute(
                request,
                step_type="outline",
                step_key="outline_v2",
                attempt=2,
                inputs={
                    "research_artifact": self._artifact_ref(research),
                    "requirements": request.requirements,
                    "previous_artifact": self._artifact_ref(outline),
                    "instruction": decision.instruction,
                },
            )
            decision = await self._wait_for_human("outline")
            if decision.action == "cancel":
                return await self._cancelled_result(request)
        if decision.action != "approve":
            return await self._waiting_result(request, "提纲需要人工处理")

        sections: dict[str, StepOutcome] = {}
        previous_sections: dict[str, dict[str, str]] = {}
        total_sections = len(outline.section_ids)
        for section_index, section_id in enumerate(outline.section_ids, start=1):
            section = await self._execute(
                request,
                step_type="section_draft",
                step_key=(
                    f"section_{section_id}_research_v2"
                    if research_recovery
                    else f"section_{section_id}_v1"
                ),
                attempt=2 if research_recovery else 1,
                inputs={
                    "section_id": section_id,
                    "outline_artifact": self._artifact_ref(outline),
                    "research_artifact": self._artifact_ref(research),
                    "requirements": request.requirements,
                    "previous_section_artifacts": previous_sections,
                    "progress": {
                        "completed_sections": section_index,
                        "total_sections": total_sections,
                    },
                },
            )
            sections[section_id] = section
            # 摘要正文仍在 Artifact 中；这里只传可校验引用，防止历史膨胀。
            previous_sections[section_id] = self._artifact_ref(section)

        if research_recovery:
            # Round 1 belongs to the report that requested more evidence.
            self._review_round = 1
        draft = await self._assemble(request, outline, sections)
        # 设置循环上限
        while self._review_round < 3:
            self._review_round += 1
            review = await self._execute(
                request,
                step_type="review",
                step_key=f"review_round_{self._review_round}",
                inputs={
                    "review_round": self._review_round,
                    "research_artifact": self._artifact_ref(research),
                    "outline_artifact": self._artifact_ref(outline),
                    "section_artifacts": {
                        key: self._artifact_ref(value) for key, value in sections.items()
                    },
                    "draft_artifact": self._artifact_ref(draft),
                },
            )
            if review.decision == "approve":
                final_decision = await self._wait_for_human("final")
                if final_decision.action == "approve":
                    final = await self._execute(
                        request,
                        step_type="finalize",
                        step_key="final_v1",
                        inputs={"draft_artifact": self._artifact_ref(draft)},
                    )
                    return WorkflowResult(
                        job_id=request.job_id,
                        status="final_approved",
                        final_artifact_uri=final.artifact_uri,
                    )
                if final_decision.action == "cancel":
                    return await self._cancelled_result(request)
                return await self._waiting_result(request, "终稿未获批准")

            if review.decision == "human_review":
                return await self._waiting_result(request, "审核要求人工介入")
            if review.decision == "research":
                return await self._waiting_result(request, "审核发现证据缺口")
            if review.decision != "rewrite" or not review.rewrite_section_ids:
                return await self._waiting_result(request, "审核结果无法自动路由")

            for section_id in review.rewrite_section_ids:
                revised = await self._execute(
                    request,
                    step_type="section_revise",
                    step_key=f"section_{section_id}_review_{self._review_round}_v2",
                    attempt=2,
                    inputs={
                        "section_id": section_id,
                        "current_artifact": self._artifact_ref(sections[section_id]),
                        "review_artifact": self._artifact_ref(review),
                        "outline_artifact": self._artifact_ref(outline),
                        "research_artifact": self._artifact_ref(research),
                        "requirements": request.requirements,
                    },
                )
                sections[section_id] = revised
            draft = await self._assemble(request, outline, sections)

        return await self._waiting_result(request, "审核轮次已达到上限 3")

    @staticmethod
    def _failure_reason(exc: Exception) -> str:
        message = str(exc).strip() or type(exc).__name__
        return f"{type(exc).__name__}: {message}"[:2000]

    async def _assemble(
        self,
        request: NewsWritingInput,
        outline: StepOutcome,
        sections: dict[str, StepOutcome],
    ) -> StepOutcome:
        return await self._execute(
            request,
            step_type="assemble",
            step_key=f"draft_review_{self._review_round}_v1",
            inputs={
                "outline_artifact": self._artifact_ref(outline),
                "section_artifacts": {
                    key: self._artifact_ref(value) for key, value in sections.items()
                },
            },
        )
    
    # 执行函数(新闻写作请求)
    async def _execute(
        self,
        request: NewsWritingInput,
        *,
        step_type: str,
        step_key: str,
        inputs: dict,
        attempt: int = 1,
    ) -> StepOutcome:
        self._phase = step_type
        result = await workflow.execute_activity(
            "run_news_step",
            StepCommand(
                tenant_id=request.tenant_id,
                job_id=request.job_id,
                step_type=step_type,
                step_key=step_key,
                attempt=attempt,
                inputs=inputs,
            ),
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=ACTIVITY_RETRY_POLICY,
            result_type=StepOutcome,
        )
        self._completed_steps += 1
        self._last_artifact_uri = result.artifact_uri
        return result

    # 等待人工请求
    async def _wait_for_human(self, gate: str) -> HumanDecision:
        self._waiting_gate = gate
        self._human_decision = None
        await workflow.wait_condition(lambda: self._human_decision is not None)
        decision = self._human_decision
        self._waiting_gate = None
        if decision is None:  # 仅用于静态类型收窄
            raise RuntimeError("human decision missing")
        return decision

    @workflow.signal
    def submit_human_decision(self, decision: HumanDecision) -> None:
        if decision.gate != self._waiting_gate:
            return
        self._human_decision = decision

    @workflow.query
    def snapshot(self) -> WorkflowSnapshot:
        return WorkflowSnapshot(
            phase=self._phase,
            completed_steps=self._completed_steps,
            review_round=self._review_round,
            waiting_gate=self._waiting_gate,
            last_artifact_uri=self._last_artifact_uri,
        )
    # 等待结果
    async def _waiting_result(
        self, request: NewsWritingInput, reason: str
    ) -> WorkflowResult:
        await self._transition_state(request, "waiting_human", reason)
        self._phase = "waiting_human"
        return WorkflowResult(
            job_id=request.job_id,
            status="waiting_human",
            reason=reason,
        )

    async def _cancelled_result(self, request: NewsWritingInput) -> WorkflowResult:
        await self._transition_state(request, "cancelled")
        self._phase = "cancelled"
        return WorkflowResult(job_id=request.job_id, status="cancelled")

    async def _transition_state(
        self,
        request: NewsWritingInput,
        target_status: str,
        reason: str | None = None,
    ) -> None:
        await workflow.execute_activity(
            "transition_job_state",
            JobStateCommand(
                tenant_id=request.tenant_id,
                job_id=request.job_id,
                target_status=target_status,
                reason=reason,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=ACTIVITY_RETRY_POLICY,
        )

    @staticmethod
    def _artifact_ref(outcome: StepOutcome) -> dict[str, str]:
        return {
            "storage_uri": outcome.artifact_uri,
            "content_sha256": outcome.content_sha256,
        }
