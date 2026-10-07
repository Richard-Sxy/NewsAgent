from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from app.schemas.hot_news import PromptMemoryContext
from app.schemas.user_memory import ResolvedMemoryContext
from app.services.memory_aware_hot_news_analysis import (
    MemoryAwareHotNewsAnalysisService,
)
from app.services.memory_prompt import MemoryPromptBuildResult


NOW = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_resolves_builds_and_injects_memory_in_order() -> None:
    item = object()
    session = object()
    resolved_context = ResolvedMemoryContext(
        tenant_id="tenant-1",
        user_id="user-1",
        task_id="task-1",
        team_id="team-news",
        section_id="finance",
        role_id="reviewer",
        resolved_at=NOW,
        memories=(),
    )
    prompt_context = PromptMemoryContext(
        resolver_policy_version="memory-prompt-v1",
        resolved_at=NOW,
    )
    analysis_execution = SimpleNamespace(analysis_input=object())
    memory_context_service = SimpleNamespace(
        resolve_for_task=AsyncMock(return_value=resolved_context)
    )
    omitted_memory_id = UUID(int=9)
    memory_prompt_builder = SimpleNamespace(
        build_with_audit=Mock(
            return_value=MemoryPromptBuildResult(
                prompt_context=prompt_context,
                omitted_memory_ids=(omitted_memory_id,),
            )
        )
    )
    analysis_service = SimpleNamespace(
        analyze_with_snapshot=AsyncMock(
            return_value=analysis_execution
        )
    )
    service = MemoryAwareHotNewsAnalysisService(
        memory_context_service=memory_context_service,
        memory_prompt_builder=memory_prompt_builder,
        analysis_service=analysis_service,
    )

    result = await service.analyze_with_memory(
        session,
        item=item,
        tenant_id="tenant-1",
        user_id="user-1",
        task_id="task-1",
        now=NOW,
        team_id="team-news",
        section_id="finance",
        role_id="reviewer",
    )

    memory_context_service.resolve_for_task.assert_awaited_once_with(
        session,
        tenant_id="tenant-1",
        user_id="user-1",
        task_id="task-1",
        now=NOW,
        team_id="team-news",
        section_id="finance",
        role_id="reviewer",
    )
    memory_prompt_builder.build_with_audit.assert_called_once_with(
        resolved_context
    )
    analysis_service.analyze_with_snapshot.assert_awaited_once_with(
        item,
        memory_context=prompt_context,
    )
    assert result.resolved_memory_context is resolved_context
    assert result.prompt_memory_context is prompt_context
    assert result.omitted_memory_ids == (omitted_memory_id,)
    assert result.analysis_execution is analysis_execution


@pytest.mark.asyncio
async def test_memory_resolution_failure_stops_model_call() -> None:
    memory_context_service = SimpleNamespace(
        resolve_for_task=AsyncMock(
            side_effect=RuntimeError("memory unavailable")
        )
    )
    memory_prompt_builder = SimpleNamespace(build_with_audit=Mock())
    analysis_service = SimpleNamespace(
        analyze_with_snapshot=AsyncMock()
    )
    service = MemoryAwareHotNewsAnalysisService(
        memory_context_service=memory_context_service,
        memory_prompt_builder=memory_prompt_builder,
        analysis_service=analysis_service,
    )

    with pytest.raises(RuntimeError, match="memory unavailable"):
        await service.analyze_with_memory(
            object(),
            item=object(),
            tenant_id="tenant-1",
            user_id="user-1",
            task_id="task-1",
            now=NOW,
        )

    memory_prompt_builder.build_with_audit.assert_not_called()
    analysis_service.analyze_with_snapshot.assert_not_awaited()
