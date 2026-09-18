import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.service import RPCError

from app.api.dependencies import (
    get_artifact_store,
    get_cms_publisher,
    get_job_service,
    get_orchestrator,
    get_recovery_service,
    get_session,
    get_tenant_id,
    get_user_id,
)
from app.clients.cms import CmsNotConfiguredError, CmsPublishError, CmsPublisher
from app.domain.execution import ArtifactType
from app.domain.job_status import JobStatus
from app.domain.transition import validate_transition
from app.models.artifact import WritingArtifact
from app.models.job import WritingJob
from app.schemas.job import (
    CreateWritingJobRequest,
    DecisionAcceptedResponse,
    HumanDecisionRequest,
    PublishJobRequest,
    PublishJobResponse,
    RecoveryPlanResponse,
    ResearchMetricsResponse,
    ResumeJobRequest,
    ResumeJobResponse,
    WritingJobListResponse,
    WorkflowProgressResponse,
    WritingJobResponse,
)
from app.schemas.research import ResearchPackage
from app.schemas.writing import ArticleDraft
from app.services.job import JobNotFoundError, JobService
from app.services.orchestrator import (
    InvalidHumanDecisionError,
    OrchestratorService,
    WorkflowExecutionNotFoundError,
)
from app.services.outbox import OutboxService
from app.services.recovery import RecoveryNotAllowedError, RecoveryService
from app.storage.s3 import S3ArtifactStore
from app.workflows.contracts import HumanDecision

router = APIRouter(prefix="/api/v1/jobs", tags=["writing-jobs"])


async def _get_job_or_404(
    session: AsyncSession,
    service: JobService,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
):
    try:
        return await service.get(session, tenant_id=tenant_id, job_id=job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _latest_artifact(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    artifact_type: ArtifactType,
) -> WritingArtifact | None:
    result = await session.execute(
        select(WritingArtifact)
        .where(
            WritingArtifact.job_id == job_id,
            WritingArtifact.artifact_type == artifact_type,
        )
        .order_by(WritingArtifact.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _load_research_package(
    session: AsyncSession,
    artifact_store: S3ArtifactStore,
    *,
    job_id: uuid.UUID,
) -> tuple[ResearchPackage, WritingArtifact]:
    artifact = await _latest_artifact(
        session, job_id=job_id, artifact_type=ArtifactType.RESEARCH_PACKAGE
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Research Artifact 尚未生成")
    payload = await artifact_store.get_json(
        storage_uri=artifact.storage_uri,
        expected_sha256=artifact.content_sha256,
    )
    return ResearchPackage.model_validate(payload), artifact


def _research_package_markdown(package: ResearchPackage) -> str:
    """生成便于运营流转的 Markdown，保留事实 ID 和原始来源。"""
    lines = [f"# {package.topic}", "", "## 关键事实", ""]
    if package.facts:
        for fact in package.facts:
            published = (
                fact.published_at.isoformat() if fact.published_at else "时间未提供"
            )
            lines.extend(
                [
                    f"### {fact.fact_id} · {fact.claim}",
                    "",
                    fact.evidence,
                    "",
                    f"- 来源：[{fact.source_title}]({fact.source_url})",
                    f"- 发布时间：{published}",
                    f"- 置信度：{fact.confidence:.0%}",
                    "",
                ]
            )
    else:
        lines.extend(["暂无可追溯事实。", ""])

    lines.extend(["## 事件时间线", ""])
    lines.extend(
        f"- **{event.occurred_at.isoformat()}** {event.description} "
        f"`支撑事实: {', '.join(event.supporting_fact_ids) or '无'}`"
        for event in package.timeline
    )
    if not package.timeline:
        lines.append("暂无时间线事件。")

    lines.extend(["", "## 证据冲突", ""])
    for conflict in package.conflicts:
        resolution = conflict.resolution or "待运营核实"
        lines.append(
            f"- **{conflict.conflict_id}** {conflict.description} "
            f"`事实: {', '.join(conflict.fact_ids)}`；处理：{resolution}"
        )
    if not package.conflicts:
        lines.append("未发现已知证据冲突。")

    lines.extend(["", "## 待核实问题", ""])
    for gap in package.evidence_gaps:
        query = f"；建议检索：{gap.suggested_query}" if gap.suggested_query else ""
        lines.append(
            f"- **[{gap.importance.upper()}] {gap.gap_id}** {gap.question}{query}"
        )
    if not package.evidence_gaps:
        lines.append("暂无已识别的证据缺口。")

    lines.extend(["", "## 候选选题角度", ""])
    for angle in package.suggested_angles:
        lines.extend(
            [
                f"### {angle.angle_id} · {angle.title}",
                "",
                angle.rationale,
                "",
                f"支撑事实：{', '.join(angle.supporting_fact_ids) or '无'}",
                "",
            ]
        )
    if not package.suggested_angles:
        lines.append("暂无候选角度。")
    return "\n".join(lines).rstrip() + "\n"


@router.post("", response_model=WritingJobResponse)
async def create_job(
    request: CreateWritingJobRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    user_id: uuid.UUID = Depends(get_user_id),
    jobs: JobService = Depends(get_job_service),
    orchestrator: OrchestratorService = Depends(get_orchestrator),
) -> WritingJobResponse:
    job, created = await jobs.create_or_get(
        session,
        tenant_id=tenant_id,
        created_by=user_id,
        request=request,
    )
    await session.commit()
    if created or job.status.value == "created":
        try:
            await orchestrator.start_job(job)
        except RPCError as exc:
            raise HTTPException(
                status_code=503,
                detail="Temporal 暂时不可用，请使用相同 idempotency_key 重试",
            ) from exc
    response.status_code = 201 if created else 200
    return WritingJobResponse.model_validate(job)


@router.get("", response_model=WritingJobListResponse)
async def list_jobs(
    statuses: list[JobStatus] | None = Query(default=None, alias="status"),
    waiting_human_only: bool = Query(default=False),
    query: str | None = Query(default=None, min_length=1, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    jobs: JobService = Depends(get_job_service),
) -> WritingJobListResponse:
    """供运营工作台按状态、待人工处理和主题筛选任务。"""
    items, total = await jobs.list_for_operations(
        session,
        tenant_id=tenant_id,
        statuses=statuses,
        waiting_human_only=waiting_human_only,
        query=query,
        limit=limit,
        offset=offset,
    )
    return WritingJobListResponse(
        items=[WritingJobResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}", response_model=WritingJobResponse)
async def get_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    jobs: JobService = Depends(get_job_service),
) -> WritingJobResponse:
    return WritingJobResponse.model_validate(
        await _get_job_or_404(session, jobs, tenant_id, job_id)
    )


@router.get("/{job_id}/progress", response_model=WorkflowProgressResponse)
async def get_progress(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    jobs: JobService = Depends(get_job_service),
    orchestrator: OrchestratorService = Depends(get_orchestrator),
) -> WorkflowProgressResponse:
    job = await _get_job_or_404(session, jobs, tenant_id, job_id)
    try:
        snapshot = await orchestrator.get_progress(job.temporal_workflow_id)
    except WorkflowExecutionNotFoundError:
        return WorkflowProgressResponse(
            job_id=job.id,
            status=job.status,
            current_step=job.current_step,
            workflow=None,
            workflow_status="missing",
        )
    except RPCError as exc:
        raise HTTPException(status_code=503, detail="Temporal 进度暂不可用") from exc
    return WorkflowProgressResponse(
        job_id=job.id,
        status=job.status,
        current_step=job.current_step,
        workflow=snapshot,
        workflow_status="available",
    )


@router.get("/{job_id}/research-metrics", response_model=ResearchMetricsResponse)
async def get_research_metrics(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    jobs: JobService = Depends(get_job_service),
    artifact_store: S3ArtifactStore = Depends(get_artifact_store),
) -> ResearchMetricsResponse:
    await _get_job_or_404(session, jobs, tenant_id, job_id)
    package, artifact = await _load_research_package(
        session, artifact_store, job_id=job_id
    )
    if package.metrics is None:
        raise HTTPException(status_code=500, detail="Research 指标计算失败")
    return ResearchMetricsResponse(
        job_id=job_id,
        logical_key=artifact.logical_key,
        artifact_version=artifact.version,
        metrics=package.metrics,
    )


@router.get("/{job_id}/research-package", response_model=ResearchPackage)
async def get_research_package(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    jobs: JobService = Depends(get_job_service),
    artifact_store: S3ArtifactStore = Depends(get_artifact_store),
) -> ResearchPackage:
    """返回经 Schema 校验和证据隔离的最新运营资料包。"""
    await _get_job_or_404(session, jobs, tenant_id, job_id)
    package, _ = await _load_research_package(
        session, artifact_store, job_id=job_id
    )
    return package


@router.get("/{job_id}/research-package/export")
async def export_research_package(
    job_id: uuid.UUID,
    format: Literal["json", "markdown"] = Query(default="markdown"),
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    jobs: JobService = Depends(get_job_service),
    artifact_store: S3ArtifactStore = Depends(get_artifact_store),
) -> Response:
    await _get_job_or_404(session, jobs, tenant_id, job_id)
    package, _ = await _load_research_package(
        session, artifact_store, job_id=job_id
    )
    filename = f"research-package-{job_id}"
    if format == "json":
        return Response(
            content=package.model_dump_json(),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
        )
    return PlainTextResponse(
        content=_research_package_markdown(package),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.md"'},
    )


@router.get("/{job_id}/export")
async def export_final_article(
    job_id: uuid.UUID,
    format: Literal["json", "markdown"] = Query(default="markdown"),
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    jobs: JobService = Depends(get_job_service),
    artifact_store: S3ArtifactStore = Depends(get_artifact_store),
) -> Response:
    await _get_job_or_404(session, jobs, tenant_id, job_id)
    artifact = await _latest_artifact(
        session, job_id=job_id, artifact_type=ArtifactType.FINAL_ARTICLE
    )
    if artifact is None:
        raise HTTPException(status_code=409, detail="任务尚未生成可导出终稿")
    payload = await artifact_store.get_json(
        storage_uri=artifact.storage_uri,
        expected_sha256=artifact.content_sha256,
    )
    article = ArticleDraft.model_validate(payload)
    filename = f"news-{job_id}"
    if format == "json":
        return Response(
            content=json.dumps(article.model_dump(mode="json"), ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
        )
    return PlainTextResponse(
        content=f"# {article.title}\n\n{article.content}\n",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.md"'},
    )


@router.get("/{job_id}/recovery", response_model=RecoveryPlanResponse)
async def get_recovery_plan(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    jobs: JobService = Depends(get_job_service),
    recovery: RecoveryService = Depends(get_recovery_service),
) -> RecoveryPlanResponse:
    await _get_job_or_404(session, jobs, tenant_id, job_id)
    return await recovery.build_recovery_plan(session, tenant_id, job_id)


@router.post("/{job_id}/resume", response_model=ResumeJobResponse, status_code=202)
async def resume_job(
    job_id: uuid.UUID,
    request: ResumeJobRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    jobs: JobService = Depends(get_job_service),
    recovery: RecoveryService = Depends(get_recovery_service),
    orchestrator: OrchestratorService = Depends(get_orchestrator),
) -> ResumeJobResponse:
    await _get_job_or_404(session, jobs, tenant_id, job_id)
    try:
        job = await recovery.prepare_resume(
            session, tenant_id, job_id, request.resume_token
        )
        await session.commit()
        await orchestrator.start_job(
            job,
            recovery_action=request.recovery_action,
            recovery_instruction=request.instruction,
        )
    except RecoveryNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RPCError as exc:
        raise HTTPException(
            status_code=503,
            detail="恢复 Workflow 暂时无法启动，请使用相同 resume_token 重试",
        ) from exc
    return ResumeJobResponse(
        job_id=job.id,
        workflow_id=job.temporal_workflow_id,
        status=job.status,
    )


@router.post("/{job_id}/decisions", response_model=DecisionAcceptedResponse, status_code=202)
async def submit_decision(
    job_id: uuid.UUID,
    request: HumanDecisionRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    jobs: JobService = Depends(get_job_service),
    orchestrator: OrchestratorService = Depends(get_orchestrator),
) -> DecisionAcceptedResponse:
    job = await _get_job_or_404(session, jobs, tenant_id, job_id)
    try:
        await orchestrator.submit_human_decision(
            job.temporal_workflow_id,
            HumanDecision(**request.model_dump()),
        )
    except InvalidHumanDecisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkflowExecutionNotFoundError as exc:
        raise HTTPException(
            status_code=409,
            detail="Temporal 工作流不存在；请先在恢复面板从检查点恢复任务，再提交人工决策",
        ) from exc
    except RPCError as exc:
        raise HTTPException(status_code=503, detail="人工决策暂时无法提交") from exc
    return DecisionAcceptedResponse(job_id=job.id, gate=request.gate)


@router.post("/{job_id}/publish", response_model=PublishJobResponse)
async def publish_final_article(
    job_id: uuid.UUID,
    request: PublishJobRequest,
    session: AsyncSession = Depends(get_session),
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    user_id: uuid.UUID = Depends(get_user_id),
    jobs: JobService = Depends(get_job_service),
    artifact_store: S3ArtifactStore = Depends(get_artifact_store),
    publisher: CmsPublisher = Depends(get_cms_publisher),
) -> PublishJobResponse:
    del user_id
    await _get_job_or_404(session, jobs, tenant_id, job_id)
    locked = await session.execute(
        select(WritingJob)
        .where(WritingJob.id == job_id, WritingJob.tenant_id == tenant_id)
        .with_for_update()
    )
    job = locked.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="WritingJob 不存在")
    if job.status == JobStatus.PUBLISHED:
        if not job.external_publication_id or not job.published_at:
            raise HTTPException(status_code=500, detail="发布状态数据不完整")
        return PublishJobResponse(
            job_id=job.id,
            status=job.status,
            external_publication_id=job.external_publication_id,
            published_at=job.published_at,
            idempotent_replay=True,
        )
    if job.status != JobStatus.FINAL_APPROVED:
        raise HTTPException(status_code=409, detail="只有已批准终稿可以发布")
    artifact = await _latest_artifact(
        session, job_id=job_id, artifact_type=ArtifactType.FINAL_ARTICLE
    )
    if artifact is None:
        raise HTTPException(status_code=409, detail="终稿 Artifact 不存在")
    payload = await artifact_store.get_json(
        storage_uri=artifact.storage_uri,
        expected_sha256=artifact.content_sha256,
    )
    article = ArticleDraft.model_validate(payload)
    try:
        external_id = await publisher.publish(
            job_id=job.id,
            tenant_id=tenant_id,
            channel=request.channel,
            article=article.model_dump(mode="json"),
        )
    except CmsNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CmsPublishError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    validate_transition(job.status, JobStatus.PUBLISHED)
    published_at = datetime.now(timezone.utc)
    job.status = JobStatus.PUBLISHED
    job.current_step = None
    job.progress_percent = 100
    job.external_publication_id = external_id
    job.published_at = published_at
    await OutboxService().enqueue_job_event(
        session,
        event_name="job.published",
        tenant_id=tenant_id,
        job=job,
        deduplication_key=f"{job.id}:published:{request.channel}",
        occurred_at=published_at,
    )
    await session.commit()
    return PublishJobResponse(
        job_id=job.id,
        status=job.status,
        external_publication_id=external_id,
        published_at=published_at,
    )
