"""组合 Memory Context 与热点分析 Agent 的应用服务。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.hot_news_enrichment import EnrichedHotNews
from app.schemas.hot_news import PromptMemoryContext
from app.schemas.user_memory import ResolvedMemoryContext
from app.services.hot_news_analysis import (
    HotNewsAnalysisExecution,
    HotNewsAnalysisService,
)
from app.services.memory_context_application import (
    MemoryContextApplicationService,
)
from app.services.memory_prompt import MemoryPromptInputBuilder


@dataclass(frozen=True, slots=True)
class MemoryAwareHotNewsAnalysisExecution:
    """保留完整解析上下文、模型上下文和最终分析执行快照。"""

    resolved_memory_context: ResolvedMemoryContext
    prompt_memory_context: PromptMemoryContext
    omitted_memory_ids: tuple[UUID, ...]
    analysis_execution: HotNewsAnalysisExecution


class MemoryAwareHotNewsAnalysisService:
    """有可信用户和任务上下文时使用的热点分析统一入口。"""

    def __init__(
        self,
        *,
        memory_context_service: MemoryContextApplicationService,
        memory_prompt_builder: MemoryPromptInputBuilder,
        analysis_service: HotNewsAnalysisService,
    ) -> None:
        self._memory_context_service = memory_context_service
        self._memory_prompt_builder = memory_prompt_builder
        self._analysis_service = analysis_service

    async def analyze_with_memory(
        self,
        session: AsyncSession,
        *,
        item: EnrichedHotNews,
        tenant_id: str,
        user_id: str,
        task_id: str,
        now: datetime,
        team_id: str | None = None,
        section_id: str | None = None,
        role_id: str | None = None,
    ) -> MemoryAwareHotNewsAnalysisExecution:
        """解析当前记忆、裁剪 Prompt，并执行一次结构化热点分析。"""

        resolved_context = (
            await self._memory_context_service.resolve_for_task(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
                now=now,
                team_id=team_id,
                section_id=section_id,
                role_id=role_id,
            )
        )
        prompt_build = self._memory_prompt_builder.build_with_audit(
            resolved_context
        )
        analysis_execution = (
            await self._analysis_service.analyze_with_snapshot(
                item,
                memory_context=prompt_build.prompt_context,
            )
        )

        return MemoryAwareHotNewsAnalysisExecution(
            resolved_memory_context=resolved_context,
            prompt_memory_context=prompt_build.prompt_context,
            omitted_memory_ids=prompt_build.omitted_memory_ids,
            analysis_execution=analysis_execution,
        )
