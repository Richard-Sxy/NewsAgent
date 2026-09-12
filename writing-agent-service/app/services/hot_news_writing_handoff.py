"""热点分析报告转交研究/写作流程的应用服务。

运营在热点控制台选定一条已校验的热点分析后，可将其转交现有的
``NewsWritingWorkflow``。转交只读取不可变的热点运行快照，把标题、
权威指标、分析结论和证据 ``news_id`` 作为可追溯的 ``requirements`` 写入
新的 ``WritingJob``，不修改热点运行本身，也不重新调用热点模型。

幂等键由 ``tenant_id + run_id + news_id + scenario`` 确定性派生，
同一热点同一场景重复转交只会得到一个写作任务。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.job_scenario import JobScenario
from app.models.job import WritingJob
from app.schemas.hot_news_memory import HotNewsAnalysisMemory
from app.schemas.job import CreateWritingJobRequest
from app.services.job import JobService


HANDOFF_IDEMPOTENCY_PREFIX = "hot-news-handoff-"


class HotNewsAnalysisNotFoundError(LookupError):
    """指定运行中不存在该 news_id 的已校验分析。"""


class HotNewsAnalysisMemoryProvider(Protocol):
    """读取单条已校验热点分析快照的端口。"""

    async def get_analysis_memory(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        news_id: str,
    ) -> HotNewsAnalysisMemory | None: ...


class WritingJobStarter(Protocol):
    """启动写作工作流的端口，通常由 OrchestratorService 实现。"""

    async def start_job(self, job: WritingJob) -> None: ...


@dataclass(frozen=True, slots=True)
class HotNewsWritingHandoffOutcome:
    job: WritingJob
    created: bool


class HotNewsWritingHandoffService:
    """把热点分析快照确定性映射为一个写作任务。"""

    def __init__(
        self,
        *,
        run_store: HotNewsAnalysisMemoryProvider,
        job_service: JobService,
        orchestrator: WritingJobStarter,
    ) -> None:
        self._run_store = run_store
        self._job_service = job_service
        self._orchestrator = orchestrator

    async def handoff(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        created_by: UUID,
        run_id: UUID,
        news_id: str,
        scenario: JobScenario = JobScenario.ASSISTED_WRITING,
    ) -> HotNewsWritingHandoffOutcome:
        memory = await self._run_store.get_analysis_memory(
            tenant_id=str(tenant_id),
            run_id=run_id,
            news_id=news_id,
        )
        if memory is None:
            raise HotNewsAnalysisNotFoundError(
                "热点运行中不存在该新闻的已校验分析"
            )

        request = self._build_request(memory=memory, scenario=scenario)
        job, created = await self._job_service.create_or_get(
            session,
            tenant_id=tenant_id,
            created_by=created_by,
            request=request,
        )
        # 工作流必须在任务行可见后启动；重试相同幂等键仍可继续启动。
        await session.commit()
        if created or job.status.value == "created":
            await self._orchestrator.start_job(job)
        return HotNewsWritingHandoffOutcome(job=job, created=created)

    @staticmethod
    def _build_request(
        *,
        memory: HotNewsAnalysisMemory,
        scenario: JobScenario,
    ) -> CreateWritingJobRequest:
        analysis_input = memory.analysis_input
        report = memory.analysis_report
        topic = analysis_input.title.strip()[:2000] or f"热点新闻 {memory.news_id}"
        return CreateWritingJobRequest(
            topic=topic,
            scenario=scenario,
            requirements={
                "source": "hot_news",
                "hot_news": HotNewsWritingHandoffService._hot_news_payload(
                    memory=memory,
                    analysis_input=analysis_input,
                    report=report,
                ),
            },
            idempotency_key=HotNewsWritingHandoffService._idempotency_key(
                tenant_id=memory.tenant_id,
                run_id=str(memory.run_id),
                news_id=memory.news_id,
                scenario=scenario,
            ),
        )

    @staticmethod
    def _hot_news_payload(
        *,
        memory: HotNewsAnalysisMemory,
        analysis_input: Any,
        report: Any,
    ) -> dict[str, Any]:
        return {
            "run_id": str(memory.run_id),
            "run_idempotency_key": memory.run_idempotency_key,
            "news_id": memory.news_id,
            "rank": memory.rank,
            "production_bundle_version": memory.production_bundle_version,
            "workflow_version": memory.workflow_version,
            "window_start": analysis_input.window_start.isoformat(),
            "window_end": analysis_input.window_end.isoformat(),
            "content_type": analysis_input.content_type,
            "hot_score": analysis_input.hot_score,
            "metrics": analysis_input.metrics.model_dump(mode="json"),
            "score_components": analysis_input.score_components.model_dump(
                mode="json"
            ),
            "trend_assessment": report.trend_assessment,
            "dominant_driver": report.dominant_driver,
            "attention_reasons": [
                reason.model_dump(mode="json")
                for reason in report.attention_reasons
            ],
            "operation_suggestions": [
                suggestion.model_dump(mode="json")
                for suggestion in report.operation_suggestions
            ],
            "limitations": list(report.limitations),
            "evidence_news_ids": list(report.evidence_news_ids),
            "overall_confidence": report.overall_confidence,
            "content_excerpt": analysis_input.content_excerpt,
            "related_news": [
                related.model_dump(mode="json")
                for related in analysis_input.related_news
            ],
            "fastgpt_request_id": memory.fastgpt_request_id,
            "validated_at": memory.validated_at.isoformat(),
        }

    @staticmethod
    def _idempotency_key(
        *,
        tenant_id: str,
        run_id: str,
        news_id: str,
        scenario: JobScenario,
    ) -> str:
        identity = "\x1f".join(
            (tenant_id, run_id, news_id, scenario.value)
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{HANDOFF_IDEMPOTENCY_PREFIX}{digest}"
