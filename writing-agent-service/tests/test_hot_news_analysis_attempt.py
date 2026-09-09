from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.errors import (
    AgentOutputValidationError,
    HotNewsAnalysisAttemptError,
)
from app.services.hot_news_analysis import HotNewsAnalysisService


@pytest.mark.asyncio
async def test_model_validation_failure_carries_trusted_input_snapshot() -> None:
    analysis_input = Mock(name="analysis_input")
    snapshot = object()
    analysis_input.model_copy.return_value = snapshot
    input_builder = Mock()
    input_builder.build.return_value = analysis_input
    runner = AsyncMock()
    runner.run.side_effect = AgentOutputValidationError(
        "invalid model JSON",
        raw_content="{broken}",
        request_id="request-1",
    )
    service = HotNewsAnalysisService(
        input_builder=input_builder,
        runner=runner,
        validator=AsyncMock(),
    )

    with pytest.raises(HotNewsAnalysisAttemptError) as captured:
        await service.analyze_with_snapshot(object())

    assert captured.value.analysis_input is snapshot
    assert captured.value.raw_content == "{broken}"
    assert captured.value.request_id == "request-1"
    assert captured.value.retryable is False
