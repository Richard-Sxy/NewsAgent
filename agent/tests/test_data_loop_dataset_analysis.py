"""Frozen-case coverage remains distinct from complete source-run statistics."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.data_analysis.engine import analyze
from app.data_analysis.runner import AnalysisExecutionError, AnalysisRunner
from app.domain.errors import HotNewsPersistenceError
from app.schemas.evaluation_dataset import (
    EvaluationDatasetManifest,
    EvaluationExpectedLabel,
    EvaluationSourceLineage,
    FrozenEvaluationCase,
    canonical_json_sha256,
)
from app.schemas.hot_news import HotNewsAnalysisInput, HotNewsMetrics, HotScoreComponents
from app.schemas.hot_news_api import (
    HotNewsMetricSnapshotView, HotNewsRankedItemView, HotNewsRunDetailResponse,
    HotNewsRunSummary, HotScoreView,
)
from app.services.data_loop.dataset_analysis import (
    DATASET_ANALYSIS_REPORT_VERSION,
    DataLoopDatasetAnalysisService,
)


TENANT = "tenant-1"
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
START = datetime(2026, 10, 3, 10, tzinfo=UTC)
END = START + timedelta(hours=1)


def detail(*, run_id=RUN_ID, start=START, bundle="bundle-v1", count=3):
    rows = tuple(HotNewsRankedItemView(
        rank=index, news_id=f"news-{index}", title="UNTRUSTED-TITLE",
        metrics=HotNewsMetricSnapshotView(
            news_id=f"news-{index}", content_type="article",
            window_start=start, window_end=start + timedelta(hours=1),
            impressions=100 * index, clicks=10 * index, unique_users=7 * index,
            total_duration_seconds=140 * index, effective_consumptions=5 * index,
            interactions=3 * index, ctr="0.1000",
        ),
        hot_score=HotScoreView(score="0.5", click_component="0.1",
            consumption_component="0.2", interaction_component="0.3", growth_component="0.4"),
        baseline={"untrusted": "UNTRUSTED-BASELINE"}, analysis=None,
    ) for index in range(1, count + 1))
    return HotNewsRunDetailResponse(
        run=HotNewsRunSummary(
            run_id=run_id, idempotency_key=f"source:{run_id}", window_start=start,
            window_end=start + timedelta(hours=1), production_bundle_version=bundle,
            workflow_version="workflow-v1", status="completed", fetched_record_count=count,
            metric_snapshot_count=count, ranked_news_count=count, analyzed_news_count=count,
            completed_at=start + timedelta(hours=1),
        ),
        ranked_news=rows, decisions=(),
    )


def case(index=1, *, source=None, news_id="news-1", run_id=RUN_ID):
    source = source or detail(run_id=run_id or RUN_ID)
    item = next(row for row in source.ranked_news if row.news_id == news_id)
    input_snapshot = HotNewsAnalysisInput(
        news_id=news_id, title="UNTRUSTED-TITLE", summary="UNTRUSTED-SUMMARY",
        content_excerpt="UNTRUSTED-BODY", content_type="article",
        window_start=source.run.window_start, window_end=source.run.window_end,
        hot_score=float(Decimal(item.hot_score.score)),
        metrics=HotNewsMetrics(**{
            key: (float(Decimal(item.metrics.ctr)) if key == "ctr" else getattr(item.metrics, key))
            for key in ("impressions", "clicks", "ctr", "unique_users", "effective_consumptions", "interactions")
        }),
        score_components=HotScoreComponents(**{
            key: float(Decimal(getattr(item.hot_score, f"{key}_component")))
            for key in ("click", "consumption", "interaction", "growth")
        }),
        analysis_policy_version="prompt-v1",
    )
    feedback_id = UUID(int=index)
    return FrozenEvaluationCase(
        case_id=f"feedback:{feedback_id}", feedback_case_id=feedback_id, news_id=news_id,
        layer="fresh_bad_case", severity="high", analysis_input=input_snapshot,
        observed_output={"untrusted": "UNTRUSTED-MODEL-OUTPUT"},
        expected=EvaluationExpectedLabel(verdict="incorrect", allowed_dominant_drivers=("click",),
                                         operator_comment="UNTRUSTED-OPERATOR-COMMENT"),
        lineage=EvaluationSourceLineage(
            feedback_case_id=feedback_id, feedback_content_sha256="a" * 64,
            run_id=run_id, run_idempotency_key=source.run.idempotency_key,
            source_type="operator_corrected", problem_type="analysis_incorrect",
            source_reference={"untrusted": "UNTRUSTED-REFERENCE"},
            production_bundle_version=source.run.production_bundle_version,
            occurred_at=END, recorded_at=END, label_id=UUID(int=100 + index), label_version=1,
            label_approved_by="UNTRUSTED-APPROVER", label_approved_at=END,
        ),
    )


def manifest(*cases, tenant_id=TENANT):
    return EvaluationDatasetManifest(
        dataset_id=UUID("33333333-3333-4333-8333-333333333333"), tenant_id=tenant_id,
        dataset_name="approved-bad-cases", dataset_version="v1", dataset_layer="fresh_bad_case",
        description="UNTRUSTED-DESCRIPTION", source_cutoff_at=END,
        frozen_at=END, frozen_by="UNTRUSTED-FROZEN-BY",
        cases=tuple(sorted(cases or (case(),), key=lambda item: str(item.feedback_case_id))),
    )


class NumericRunner:
    """Approved engine stand-in adds volatile metadata the report must drop."""

    def __init__(self):
        self.calls = []

    async def run(self, request):
        self.calls.append(deepcopy(request))
        return {**analyze(request), "execution": {"elapsed_ms": len(self.calls),
                    "request_id": str(uuid4()), "untrusted": "UNTRUSTED-RUNTIME"},
                "debug": "UNTRUSTED-DEBUG"}


def service(*, reader=None, runner=None, max_cases=20):
    reader = reader or SimpleNamespace(get_run_detail=AsyncMock(return_value=detail()))
    runner = runner or NumericRunner()
    return DataLoopDatasetAnalysisService(source_runs=reader, runner=runner, max_cases=max_cases), reader, runner


@pytest.mark.asyncio
async def test_full_source_run_is_analyzed_once_for_duplicate_news_feedback():
    data = manifest(case(1), case(2), case(3, news_id="news-2"))
    current, reader, runner = service()
    report = await current.analyze(tenant_id=TENANT, manifest=data)
    reader.get_run_detail.assert_awaited_once_with(tenant_id=TENANT, run_id=RUN_ID)
    assert [value["operation"] for value in runner.calls] == ["overview", "quality"]
    assert all(len(value["dataset"]["rows"]) == 3 for value in runner.calls)
    assert report["summary"] == {"case_count": 3, "source_run_count": 1, "analyzed_run_count": 1,
                                 "covered_case_count": 3, "unavailable_case_count": 0}
    source = report["source_runs"][0]
    assert len(source["case_mapping"]) == 3 and source["referenced_unique_news_count"] == 2
    assert source["ranked_news_count"] == 3
    assert source["results"]["overview"]["analysis"]["totals"]["impressions"] == 600
    assert "unique_users" not in source["results"]["overview"]["analysis"]["totals"]
    assert source["projection_sha256"] == canonical_json_sha256(source["projection"])
    assert report["scope"] == "referenced_ranked_runs"
    assert any("评测子集" in value for value in report["limitations"])


@pytest.mark.asyncio
async def test_real_fixed_process_runner_and_repeat_report_are_stable():
    current, _, _ = service(runner=AnalysisRunner(timeout_seconds=5))
    data = manifest()
    first = await current.analyze(tenant_id=TENANT, manifest=data)
    second = await current.analyze(tenant_id=TENANT, manifest=data)
    assert first == second
    assert first["analysis_status"] == "completed"
    assert first["report_version"] == DATASET_ANALYSIS_REPORT_VERSION
    assert first["dataset_sha256"] == data.content_sha256
    assert "execution" not in json.dumps(first)


@pytest.mark.asyncio
async def test_report_excludes_case_text_identity_and_volatile_runner_metadata():
    current, _, _ = service()
    first = await current.analyze(tenant_id=TENANT, manifest=manifest())
    second = await current.analyze(tenant_id=TENANT, manifest=manifest())
    assert first == second
    encoded = json.dumps(first)
    assert "UNTRUSTED" not in encoded
    assert "elapsed_ms" not in encoded and "request_id" not in encoded
    assert first["case_statistics"]["problem_types"] == {"analysis_incorrect": 1}


@pytest.mark.asyncio
async def test_unknown_free_form_lineage_categories_are_not_echoed():
    item = case()
    item = item.model_copy(update={"severity": "UNTRUSTED-SEVERITY", "lineage": item.lineage.model_copy(
        update={"source_type": "UNTRUSTED-SOURCE", "problem_type": "UNTRUSTED-PROBLEM"})})
    current, _, _ = service()
    report = await current.analyze(tenant_id=TENANT, manifest=manifest(item))
    assert "UNTRUSTED" not in json.dumps(report)
    for key in ("source_types", "problem_types", "severities"):
        assert report["case_statistics"][key] == {"unknown": 1}


@pytest.mark.asyncio
async def test_manifest_tenant_mismatch_is_refused_before_any_port():
    current, reader, runner = service()
    with pytest.raises(ValueError, match="dataset_analysis_tenant_mismatch"):
        await current.analyze(tenant_id="other-tenant", manifest=manifest())
    reader.get_run_detail.assert_not_awaited()
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_id", [True, False])
async def test_missing_source_never_invents_a_run_or_case_only_ranking(missing_id):
    reader = SimpleNamespace(get_run_detail=AsyncMock(return_value=None))
    current, _, runner = service(reader=reader)
    data = manifest(case(run_id=None) if missing_id else case())
    report = await current.analyze(tenant_id=TENANT, manifest=data)
    assert report["analysis_status"] == "degraded"
    assert report["summary"]["covered_case_count"] == 0
    assert report["summary"]["unavailable_case_count"] == 1
    source = report["source_runs"][0]
    assert source["reason_code"] == ("source_run_id_missing" if missing_id else "source_completed_run_not_found")
    assert source["projection"] is None and source["results"] == {}
    assert runner.calls == []
    if missing_id:
        reader.get_run_detail.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ({"window_start": START - timedelta(hours=1)}, "source_lineage_conflict"),
    ({"hot_score": 0.8}, "case_snapshot_conflict"),
    ({"title": "other snapshot"}, "case_snapshot_conflict"),
])
async def test_conflicting_frozen_snapshots_make_the_source_unavailable(change, reason):
    changed = case(2)
    changed = changed.model_copy(update={"analysis_input": changed.analysis_input.model_copy(update=change)})
    current, reader, runner = service()
    report = await current.analyze(tenant_id=TENANT, manifest=manifest(case(1), changed))
    assert report["analysis_status"] == "degraded"
    assert report["source_runs"][0]["reason_code"] == reason
    assert report["summary"]["unavailable_case_count"] == 2
    reader.get_run_detail.assert_not_awaited()
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,reason", [
    ("run_id", OTHER_RUN_ID, "source_identity_mismatch"),
    ("idempotency_key", "different-source", "source_identity_mismatch"),
    ("production_bundle_version", "different-bundle", "source_bundle_mismatch"),
    ("window_start", START - timedelta(hours=1), "source_window_mismatch"),
    ("status", "processing", "source_completed_run_not_found"),
    ("ranked_news_count", 2, "source_projection_invalid"),
])
async def test_source_identity_and_window_must_match_frozen_case(field, value, reason):
    source = detail()
    source = source.model_copy(update={"run": source.run.model_copy(update={field: value})})
    current, _, runner = service(reader=SimpleNamespace(get_run_detail=AsyncMock(return_value=source)))
    report = await current.analyze(tenant_id=TENANT, manifest=manifest())
    assert report["source_runs"][0]["reason_code"] == reason
    assert runner.calls == []


@pytest.mark.asyncio
async def test_reader_with_explicit_foreign_tenant_is_rejected():
    actual = detail()
    source = SimpleNamespace(tenant_id="foreign", run=actual.run, ranked_news=actual.ranked_news)
    current, _, _ = service(reader=SimpleNamespace(get_run_detail=AsyncMock(return_value=source)))
    report = await current.analyze(tenant_id=TENANT, manifest=manifest())
    assert report["source_runs"][0]["reason_code"] == "source_tenant_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("location,field,value,reason", [
    ("metrics", "clicks", 11, "source_metric_mismatch"),
    ("metrics", "ctr", "0.11", "source_metric_mismatch"),
    ("metrics", "content_type", "video", "source_metric_mismatch"),
    ("metrics", "window_start", START - timedelta(minutes=1), "source_projection_invalid"),
    ("hot_score", "score", "0.51", "source_hot_score_mismatch"),
    ("hot_score", "growth_component", "0.41", "source_hot_score_mismatch"),
])
async def test_case_metrics_and_hot_score_must_match_authoritative_source(location, field, value, reason):
    source = detail()
    row = source.ranked_news[0]
    row = row.model_copy(update={location: getattr(row, location).model_copy(update={field: value})})
    source = source.model_copy(update={"ranked_news": (row, *source.ranked_news[1:])})
    current, _, runner = service(reader=SimpleNamespace(get_run_detail=AsyncMock(return_value=source)))
    report = await current.analyze(tenant_id=TENANT, manifest=manifest())
    assert report["source_runs"][0]["reason_code"] == reason
    assert runner.calls == []


@pytest.mark.asyncio
async def test_original_decimal_to_float_conversion_matches_without_broad_tolerance():
    source = detail()
    row = source.ranked_news[0]
    row = row.model_copy(update={"hot_score": row.hot_score.model_copy(
        update={"score": "0.123456789012345678901"})})
    source = source.model_copy(update={"ranked_news": (row, *source.ranked_news[1:])})
    current, _, _ = service(reader=SimpleNamespace(get_run_detail=AsyncMock(return_value=source)))
    report = await current.analyze(tenant_id=TENANT, manifest=manifest(case(source=source)))
    assert report["analysis_status"] == "completed"
    assert report["source_runs"][0]["projection"]["rows"][0]["hot_score"] == "0.123456789012345678901"


@pytest.mark.asyncio
async def test_absent_or_duplicate_news_does_not_use_another_row():
    for rows, reason in ((detail().ranked_news[1:], "source_news_missing"),
                         ((detail().ranked_news[0],) * 3, "source_projection_invalid")):
        source = detail().model_copy(update={"ranked_news": rows})
        current, _, runner = service(reader=SimpleNamespace(get_run_detail=AsyncMock(return_value=source)))
        report = await current.analyze(tenant_id=TENANT, manifest=manifest())
        assert report["source_runs"][0]["reason_code"] == reason
        assert runner.calls == []


@pytest.mark.asyncio
async def test_source_row_limit_marks_unavailable_without_truncation_or_execution(monkeypatch):
    runner = AnalysisRunner(max_rows=2)
    execution = AsyncMock(side_effect=AssertionError("worker must not execute"))
    monkeypatch.setattr(runner, "_execute", execution)
    current, _, _ = service(runner=runner)
    report = await current.analyze(tenant_id=TENANT, manifest=manifest())
    source = report["source_runs"][0]
    assert report["analysis_status"] == "degraded" and source["reason_code"] == "analysis_row_limit"
    assert len(source["projection"]["rows"]) == 3 and source["results"] == {}
    execution.assert_not_awaited()


@pytest.mark.asyncio
async def test_case_limit_refuses_entire_dataset_before_source_query():
    current, reader, runner = service(max_cases=1)
    with pytest.raises(ValueError, match="dataset_analysis_case_limit"):
        await current.analyze(tenant_id=TENANT, manifest=manifest(case(1), case(2)))
    reader.get_run_detail.assert_not_awaited()
    assert runner.calls == []


@pytest.mark.asyncio
async def test_multiple_windows_are_analyzed_separately_without_global_metric_totals():
    earlier = detail(run_id=OTHER_RUN_ID, start=START - timedelta(hours=1))
    reader = SimpleNamespace(get_run_detail=AsyncMock(side_effect=[detail(), earlier]))
    current, _, runner = service(reader=reader)
    data = manifest(case(1), case(2, source=earlier, run_id=OTHER_RUN_ID))
    report = await current.analyze(tenant_id=TENANT, manifest=data)
    assert report["summary"]["analyzed_run_count"] == 2
    assert len(runner.calls) == 4
    assert "totals" not in report and "totals" not in report["summary"]
    assert len({row["projection"]["window_start"] for row in report["source_runs"]}) == 2


@pytest.mark.asyncio
async def test_query_database_failure_propagates_for_activity_retry():
    error = HotNewsPersistenceError("database temporarily unavailable")
    reader = SimpleNamespace(get_run_detail=AsyncMock(side_effect=error))
    current, _, runner = service(reader=reader)
    with pytest.raises(HotNewsPersistenceError) as caught:
        await current.analyze(tenant_id=TENANT, manifest=manifest())
    assert caught.value is error and caught.value.retryable
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code,reason", [("timeout", "analysis_timeout"),
                                               ("UNTRUSTED-ERROR", "analysis_execution_failed")])
async def test_runner_failure_is_fixed_reason_and_not_retried(error_code, reason):
    runner = SimpleNamespace(run=AsyncMock(side_effect=AnalysisExecutionError(error_code)))
    current, _, _ = service(runner=runner)
    report = await current.analyze(tenant_id=TENANT, manifest=manifest())
    assert report["source_runs"][0]["reason_code"] == reason
    assert report["analysis_status"] == "degraded" and "UNTRUSTED" not in json.dumps(report)
    assert runner.run.await_count == 1


@pytest.mark.asyncio
async def test_wrong_runner_source_is_not_accepted_as_evidence():
    runner = NumericRunner()
    original = runner.run

    async def changed(request):
        value = await original(request)
        value["source"]["run_id"] = str(OTHER_RUN_ID)
        return value

    runner.run = changed
    current, _, _ = service(runner=runner)
    report = await current.analyze(tenant_id=TENANT, manifest=manifest())
    assert report["source_runs"][0]["reason_code"] == "analysis_invalid_output"
    assert report["source_runs"][0]["results"] == {}


@pytest.mark.parametrize("max_cases", [0, 101, True, 1.5])
def test_invalid_service_limits_are_rejected(max_cases):
    with pytest.raises(ValueError, match="dataset_analysis_max_cases_invalid"):
        service(max_cases=max_cases)
