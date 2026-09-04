import uuid
from dataclasses import dataclass
from typing import Any

from app.clients.fastgpt import AgentResult
from app.db.session import Database
from app.domain.execution import AgentType, ArtifactType, StepType
from app.domain.errors import AgentOutputValidationError
from app.domain.job_status import JobStatus
from app.schemas.checkpoint import FailureCommit
from app.schemas.research import ResearchPackage
from app.schemas.review import ReviewInput, ReviewReport
from app.schemas.writing import (
    ArticleAssemblyInput,
    ArticleDraft,
    ArticleOutline,
    ArticleSection,
    SectionWritingInput,
)
from app.services.agents import ResearchAgentRunner, ReviewerAgentRunner, WriterAgentRunner
from app.services.artifact_pipeline import ArtifactPipeline
from app.services.checkpoint import CheckpointService
from app.services.execution import ExecutionService, PreparedExecution
from app.storage.s3 import S3ArtifactStore
from app.workflows.contracts import StepCommand, StepOutcome


@dataclass(frozen=True)
class StepPolicy:
    step_type: StepType
    agent_type: AgentType
    app_id: str
    artifact_type: ArtifactType
    active_status: JobStatus
    default_target_status: JobStatus
    next_step: StepType | None


class NewsStepHandler:
    """最重要的执行文件：执行单个 checkpoint:准备审计记录、调用 Agent、保存并提交。"""

    def __init__(
        self,
        *,
        database: Database,
        artifact_store: S3ArtifactStore,
        checkpoint_service: CheckpointService,
        execution_service: ExecutionService,
        artifact_pipeline: ArtifactPipeline,
        research_runner: ResearchAgentRunner,
        writer_runner: WriterAgentRunner,
        reviewer_runner: ReviewerAgentRunner,
        research_app_id: str,
        writer_app_id: str,
        reviewer_app_id: str,
    ) -> None:
        self.database = database
        self.artifact_store = artifact_store
        self.checkpoint_service = checkpoint_service
        self.execution_service = execution_service
        self.artifact_pipeline = artifact_pipeline
        self.research_runner = research_runner
        self.writer_runner = writer_runner
        self.reviewer_runner = reviewer_runner
        self.policies = self._build_policies(
            research_app_id, writer_app_id, reviewer_app_id
        )

    async def __call__(self, command: StepCommand) -> StepOutcome:
        policy = self._policy(command.step_type)
        tenant_id = uuid.UUID(command.tenant_id)
        job_id = uuid.UUID(command.job_id)
        prepared: PreparedExecution | None = None
        checkpoint_committed = False

        try:
            # 创建数据库连接
            async with self.database.session() as session:
                prepared = await self.execution_service.prepare(
                    session,
                    tenant_id=tenant_id,
                    job_id=job_id,
                    step_type=policy.step_type,
                    step_key=command.step_key,
                    requested_attempt=command.attempt,
                    input_snapshot=command.inputs,
                    agent_type=policy.agent_type,
                    fastgpt_app_id=policy.app_id,
                    active_status=policy.active_status,
                )
            if prepared.replay_artifact is not None:
                outcome = await self._replay_outcome(
                    prepared.replay_artifact, command
                )
                target_status, next_step = self._route_replayed_success(
                    policy, outcome
                )
                async with self.database.session() as session:
                    await self.execution_service.advance_replayed_job(
                        session,
                        tenant_id=tenant_id,
                        job_id=job_id,
                        target_status=target_status,
                        next_step=next_step,
                        review_round=(
                            command.inputs.get("review_round")
                            if policy.step_type == StepType.REVIEW
                            else None
                        ),
                    )
                return outcome

            result = await self._dispatch(command, policy)
            self._validate_agent_result(command, result)
            target_status, next_step = self._route_success(policy, result.value)
            async with self.database.session() as session:
                artifact = await self.artifact_pipeline.persist_success(
                    session=session,
                    tenant_id=tenant_id,
                    job_id=job_id,
                    step_id=prepared.step_id,
                    agent_run_id=prepared.agent_run_id,
                    result=result,
                    artifact_type=policy.artifact_type,
                    logical_key=command.step_key,
                    schema_version="1.0",
                    target_status=target_status,
                    next_step=next_step,
                    metadata={"attempt": prepared.attempt},
                    review_round=(
                        command.inputs.get("review_round")
                        if policy.step_type == StepType.REVIEW
                        else None
                    ),
                )
            checkpoint_committed = True
            return self._outcome(artifact.storage_uri, artifact.content_sha256, command, result.value)
        except Exception as exc:
            if (
                prepared is not None
                and prepared.replay_artifact is None
                and not checkpoint_committed
            ):
                await self._record_failure(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    prepared=prepared,
                    error=exc,
                )
            raise
    # 调度
    async def _dispatch(
        self,
        command: StepCommand,
        policy: StepPolicy,
    ) -> AgentResult:
        inputs = command.inputs
        if policy.step_type == StepType.RESEARCH:
            previous = None
            if inputs.get("previous_artifact"):
                previous = await self._load(
                    inputs["previous_artifact"], ResearchPackage
                )
            return await self.research_runner.run(
                job_id=command.job_id,
                topic=inputs.get("topic", ""),
                requirements=inputs.get("requirements", {}),
                previous_package=previous,
                revision_instruction=inputs.get("instruction"),
            )

        if policy.step_type == StepType.OUTLINE:
            research = await self._load(inputs["research_artifact"], ResearchPackage)
            previous_outline = None
            if inputs.get("previous_artifact"):
                previous_outline = await self._load(
                    inputs["previous_artifact"], ArticleOutline
                )
            return await self.writer_runner.create_outline(
                job_id=command.job_id,
                research_package=research,
                requirements=inputs.get("requirements", {}),
                previous_outline=previous_outline,
                revision_instruction=inputs.get("instruction"),
            )

        if policy.step_type in {StepType.SECTION_DRAFT, StepType.SECTION_REVISE}:
            outline = await self._load(inputs["outline_artifact"], ArticleOutline)
            research = await self._load(inputs["research_artifact"], ResearchPackage)
            previous = await self._load_section_summaries(
                inputs.get("previous_section_artifacts", {})
            )
            revision_instruction = None
            current_section = None
            if policy.step_type == StepType.SECTION_REVISE:
                review = await self._load(inputs["review_artifact"], ReviewReport)
                current_section = await self._load(
                    inputs["current_artifact"], ArticleSection
                )
                revision_instruction = self._revision_instruction(
                    review, inputs["section_id"]
                )
            return await self.writer_runner.write_section(
                SectionWritingInput(
                    job_id=command.job_id,
                    section_id=inputs["section_id"],
                    outline=outline,
                    research_package=research,
                    requirements=inputs.get("requirements", {}),
                    previous_sections_summary=previous,
                    current_section=current_section,
                    revision_instruction=revision_instruction,
                )
            )

        if policy.step_type == StepType.ASSEMBLE:
            outline = await self._load(inputs["outline_artifact"], ArticleOutline)
            sections = await self._load_sections(inputs["section_artifacts"])
            return await self.writer_runner.assemble(
                ArticleAssemblyInput(
                    job_id=command.job_id,
                    outline=outline,
                    sections=sections,
                )
            )

        if policy.step_type == StepType.REVIEW:
            research = await self._load(inputs["research_artifact"], ResearchPackage)
            outline = await self._load(inputs["outline_artifact"], ArticleOutline)
            sections = await self._load_sections(inputs["section_artifacts"])
            draft = await self._load(inputs["draft_artifact"], ArticleDraft)
            return await self.reviewer_runner.run(
                ReviewInput(
                    job_id=command.job_id,
                    review_round=inputs["review_round"],
                    research_package=research,
                    outline=outline,
                    sections=sections,
                    draft=draft,
                )
            )

        if policy.step_type == StepType.FINALIZE:
            draft = await self._load(inputs["draft_artifact"], ArticleDraft)
            return AgentResult(
                value=draft,
                request_id=None,
                usage={},
                raw_content=draft.model_dump_json(),
            )
        raise ValueError(f"尚未实现的 step_type: {policy.step_type.value}")

    async def _load(self, reference: dict[str, str], model_type):
        payload = await self.artifact_store.get_json(
            storage_uri=reference["storage_uri"],
            expected_sha256=reference["content_sha256"],
        )
        return model_type.model_validate(payload)

    async def _load_sections(
        self, references: dict[str, dict[str, str]]
    ) -> list[ArticleSection]:
        return [
            await self._load(reference, ArticleSection)
            for _, reference in sorted(references.items())
        ]

    async def _load_section_summaries(
        self, references: dict[str, dict[str, str]]
    ) -> dict[str, str]:
        summaries: dict[str, str] = {}
        for section_id, reference in references.items():
            section = await self._load(reference, ArticleSection)
            summaries[section_id] = section.summary
        return summaries

    async def _replay_outcome(
        self, artifact, command: StepCommand
    ) -> StepOutcome:
        payload = await self.artifact_store.get_json(
            storage_uri=artifact.storage_uri,
            expected_sha256=artifact.content_sha256,
        )
        value: Any = payload
        if artifact.artifact_type == ArtifactType.RESEARCH_PACKAGE:
            value = ResearchPackage.model_validate(payload)
            self._validate_agent_result(
                command,
                AgentResult(
                    value=value,
                    request_id=None,
                    usage={},
                    raw_content=value.model_dump_json(),
                ),
            )
        return self._outcome(
            artifact.storage_uri,
            artifact.content_sha256,
            command,
            value,
        )

    async def _record_failure(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        prepared: PreparedExecution,
        error: Exception,
    ) -> None:
        async with self.database.session() as session:
            await self.checkpoint_service.commit_failure(
                session,
                FailureCommit(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    step_id=prepared.step_id,
                    agent_run_id=prepared.agent_run_id,
                    error_code=type(error).__name__.upper()[:120],
                    error_message=str(error)[:8000] or type(error).__name__,
                    retryable=bool(getattr(error, "retryable", False)),
                ),
            )

    @staticmethod
    def _outcome(
        storage_uri: str,
        content_sha256: str,
        command: StepCommand,
        value: Any,
    ) -> StepOutcome:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        section_ids = [item["section_id"] for item in value.get("sections", [])]
        rewrite_ids = [
            item["section_id"]
            for item in value.get("section_decisions", [])
            if item["decision"] == "rewrite"
        ]
        return StepOutcome(
            artifact_uri=storage_uri,
            content_sha256=content_sha256,
            logical_key=command.step_key,
            section_ids=section_ids,
            decision=value.get("decision"),
            rewrite_section_ids=rewrite_ids,
        )

    @staticmethod
    def _revision_instruction(report: ReviewReport, section_id: str) -> str:
        issue_ids = {
            issue_id
            for decision in report.section_decisions
            if decision.section_id == section_id
            for issue_id in decision.issue_ids
        }
        messages = [
            f"{issue.issue_type}: {issue.reason}"
            for issue in report.issues
            if issue.issue_id in issue_ids
        ]
        if not messages:
            raise ValueError(f"审核报告没有提供章节 {section_id} 的返工说明")
        return "\n".join(messages)

    @staticmethod
    def _validate_agent_result(command: StepCommand, result: AgentResult) -> None:
        value = result.value
        if getattr(value, "job_id", None) != command.job_id:
            raise AgentOutputValidationError(
                "Agent 输出 job_id 与当前任务不一致",
                raw_content=result.raw_content,
                request_id=result.request_id,
            )
        if isinstance(value, ResearchPackage):
            metrics = value.metrics
            if metrics is None:
                raise AgentOutputValidationError(
                    "Research Agent 输出缺少确定性质量指标",
                    raw_content=result.raw_content,
                    request_id=result.request_id,
                )
            quality_errors: list[str] = []
            if metrics.fact_count < 3:
                quality_errors.append(f"可追溯事实不足：{metrics.fact_count} < 3")
            if metrics.unique_source_count < 2:
                quality_errors.append(f"独立来源不足：{metrics.unique_source_count} < 2")
            if metrics.citation_coverage_percent < 100:
                quality_errors.append("事实引用覆盖率未达到 100%")
            unsupported_timeline = [
                event.event_id
                for event in value.timeline
                if not event.supporting_fact_ids
            ]
            if unsupported_timeline:
                quality_errors.append(
                    "时间线缺少事实支撑："
                    + ",".join(unsupported_timeline)
                )
            unsupported_angles = [
                angle.angle_id
                for angle in value.suggested_angles
                if not angle.supporting_fact_ids
            ]
            if unsupported_angles:
                quality_errors.append(
                    "选题角度缺少事实支撑："
                    + ",".join(unsupported_angles)
                )
            if quality_errors:
                raise AgentOutputValidationError(
                    "ResearchPackage 未达到最低质量门："
                    + "；".join(quality_errors),
                    raw_content=result.raw_content,
                    request_id=result.request_id,
                )
        if isinstance(value, ArticleSection):
            if value.section_id != command.inputs.get("section_id"):
                raise AgentOutputValidationError(
                    "Agent 输出 section_id 与当前章节不一致",
                    raw_content=result.raw_content,
                    request_id=result.request_id,
                )
        if isinstance(value, ArticleDraft):
            expected = set(command.inputs.get("section_artifacts", {}))
            if command.step_type == StepType.ASSEMBLE.value and set(value.section_ids) != expected:
                raise AgentOutputValidationError(
                    "合成稿 section_ids 与输入章节集合不一致",
                    raw_content=result.raw_content,
                    request_id=result.request_id,
                )
        if isinstance(value, ReviewReport):
            if value.review_round != command.inputs.get("review_round"):
                raise AgentOutputValidationError(
                    "审核输出 review_round 与当前轮次不一致",
                    raw_content=result.raw_content,
                    request_id=result.request_id,
                )
            known_sections = set(command.inputs.get("section_artifacts", {}))
            reviewed_sections = {
                decision.section_id for decision in value.section_decisions
            }
            if reviewed_sections - known_sections:
                raise AgentOutputValidationError(
                    "审核输出包含未知 section_id",
                    raw_content=result.raw_content,
                    request_id=result.request_id,
                )

    
    def _policy(self, raw_step_type: str) -> StepPolicy:
        try:
            return self.policies[StepType(raw_step_type)]
        except (ValueError, KeyError) as exc:
            raise ValueError(f"不支持的 step_type: {raw_step_type}") from exc

    @staticmethod
    def _route_success(policy: StepPolicy, value: Any) -> tuple[JobStatus, StepType | None]:
        if policy.step_type != StepType.REVIEW:
            return policy.default_target_status, policy.next_step
        routes = {
            "approve": (JobStatus.FINAL_REVIEW, StepType.FINALIZE),
            "rewrite": (JobStatus.REVISING, StepType.SECTION_REVISE),
            "research": (JobStatus.RESEARCHING, StepType.RESEARCH),
            "human_review": (JobStatus.WAITING_HUMAN, None),
        }
        return routes[value.decision]

    @staticmethod
    def _route_replayed_success(
        policy: StepPolicy,
        outcome: StepOutcome,
    ) -> tuple[JobStatus, StepType | None]:
        if policy.step_type != StepType.REVIEW:
            return policy.default_target_status, policy.next_step
        routes = {
            "approve": (JobStatus.FINAL_REVIEW, StepType.FINALIZE),
            "rewrite": (JobStatus.REVISING, StepType.SECTION_REVISE),
            "research": (JobStatus.RESEARCHING, StepType.RESEARCH),
            "human_review": (JobStatus.WAITING_HUMAN, None),
        }
        if outcome.decision not in routes:
            raise ValueError("重放的审核 Artifact 缺少有效 decision")
        return routes[outcome.decision]

    @staticmethod
    def _build_policies(research_app: str, writer_app: str, reviewer_app: str):
        return {
            StepType.RESEARCH: StepPolicy(StepType.RESEARCH, AgentType.RESEARCH, research_app, ArtifactType.RESEARCH_PACKAGE, JobStatus.RESEARCHING, JobStatus.RESEARCH_REVIEW, None),
            StepType.OUTLINE: StepPolicy(StepType.OUTLINE, AgentType.WRITER, writer_app, ArtifactType.OUTLINE, JobStatus.OUTLINING, JobStatus.OUTLINE_REVIEW, None),
            StepType.SECTION_DRAFT: StepPolicy(StepType.SECTION_DRAFT, AgentType.WRITER, writer_app, ArtifactType.SECTION, JobStatus.DRAFTING, JobStatus.DRAFTING, StepType.SECTION_DRAFT),
            StepType.ASSEMBLE: StepPolicy(StepType.ASSEMBLE, AgentType.WRITER, writer_app, ArtifactType.DRAFT, JobStatus.ASSEMBLING, JobStatus.REVIEWING, StepType.REVIEW),
            StepType.REVIEW: StepPolicy(StepType.REVIEW, AgentType.REVIEWER, reviewer_app, ArtifactType.REVIEW_REPORT, JobStatus.REVIEWING, JobStatus.FINAL_REVIEW, StepType.FINALIZE),
            StepType.SECTION_REVISE: StepPolicy(StepType.SECTION_REVISE, AgentType.WRITER, writer_app, ArtifactType.SECTION, JobStatus.REVISING, JobStatus.ASSEMBLING, StepType.ASSEMBLE),
            StepType.FINALIZE: StepPolicy(StepType.FINALIZE, AgentType.WRITER, writer_app, ArtifactType.FINAL_ARTICLE, JobStatus.FINAL_REVIEW, JobStatus.FINAL_APPROVED, None),
        }
