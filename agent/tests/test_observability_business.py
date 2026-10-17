"""Business quality errors remain distinct from successful structured inference."""
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.domain.errors import AgentOutputValidationError, HotNewsAnalysisAttemptError
from app.model_runtime.result import AgentResult
from app.observability import telemetry
from app.observability.settings import ObservabilitySettings
from app.modules.hot_news.analysis.hot_news_analysis import HotNewsAnalysisInputBuilder, HotNewsAnalysisService, HotNewsAnalysisValidator
from tests.test_hot_news_agent_module import analysis_input, enriched_hot_news, valid_report


def result(report):
    return AgentResult(value=report, request_id="provider-1", usage={}, raw_content="PRIVATE_OUTPUT")


@pytest.mark.parametrize("kind", ["news_id_mismatch", "memory_reference", "evidence_missing", "evidence_reference", "attention_reason"])
def test_business_failure_kind_comes_from_trusted_check_not_error_text(kind):
    original = analysis_input()
    report = valid_report()
    if kind == "news_id_mismatch":
        report = report.model_copy(update={"news_id": "PRIVATE_FORGED_ID"})
    elif kind == "memory_reference":
        report = report.model_copy(update={"applied_memory_ids": [uuid4()]})
    elif kind == "evidence_missing":
        original = original.model_copy(update={"related_news": []})
    elif kind == "evidence_reference":
        report = report.model_copy(update={"evidence_news_ids": ["PRIVATE_FORGED_ID"]})
    else:
        reason = report.attention_reasons[0].model_copy(update={"metric_keys": ["PRIVATE_METRIC"]})
        report = report.model_copy(update={"attention_reasons": [reason]})
    with pytest.raises(AgentOutputValidationError) as error:
        HotNewsAnalysisValidator().validate(analysis_input=original, result=result(report))
    assert error.value.failure_stage == "business_validation"
    assert error.value.failure_kind == kind


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", [True, False])
async def test_each_business_validation_attempt_is_visible_and_classification_survives_retry(monkeypatch, recovered):
    monkeypatch.setattr(telemetry, "_runtime", None)
    spans = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    runtime = telemetry.configure_telemetry(settings=ObservabilitySettings(enabled=True, json_logs=False),
        span_exporter=spans, metric_reader=reader, log_exporter=InMemoryLogRecordExporter())
    invalid = valid_report().model_copy(update={"news_id": "PRIVATE_FORGED_ID"})
    runner = AsyncMock()
    runner.run.side_effect = [result(invalid), result(valid_report() if recovered else invalid)]
    service = HotNewsAnalysisService(input_builder=HotNewsAnalysisInputBuilder(), runner=runner,
        validator=HotNewsAnalysisValidator())
    try:
        if recovered:
            await service.analyze_with_snapshot(enriched_hot_news())
        else:
            with pytest.raises(HotNewsAnalysisAttemptError) as caught:
                await service.analyze_with_snapshot(enriched_hot_news())
            assert caught.value.failure_stage == "business_validation"
            assert caught.value.failure_kind == "news_id_mismatch"
        recorded = spans.get_finished_spans()
        attempts = [s for s in recorded if s.name == "model.business_validate"]
        assert [s.attributes["newsagent.attempt"] for s in attempts] == [1, 2]
        assert attempts[0].status.status_code == StatusCode.ERROR
        assert (attempts[1].status.status_code != StatusCode.ERROR) == recovered
        assert all("PRIVATE_" not in s.to_json() for s in recorded)
        data = reader.get_metrics_data()
        points = [p for resource in data.resource_metrics for scope in resource.scope_metrics
                  for metric in scope.metrics if metric.name == "newsagent.operation.calls"
                  for p in metric.data.data_points if p.attributes["newsagent.operation"] == "model.business_validate"]
        errors = [p for p in points if p.attributes["newsagent.outcome"] == "error"]
        assert sum(p.value for p in errors) == (1 if recovered else 2)
    finally:
        runtime.shutdown()
