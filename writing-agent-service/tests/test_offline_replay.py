from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.clients.fastgpt import AgentResult
from app.schemas.evaluation_dataset import EvaluationDatasetManifest
from app.schemas.hot_news import AnalysisReason, HotNewsAnalysisReport
from app.schemas.production_bundle import ProductionBundleSpec
from app.services.data_loop.offline_replay import HotNewsOfflineReplayService
from tests.test_evaluation_dataset_schema import CASE_A, frozen_case


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


def spec(app_id: str) -> ProductionBundleSpec:
    return ProductionBundleSpec(
        metric_definition_version="metric-v1",
        hot_score_policy_version="score-v1",
        reranker_policy_version="rerank-v1",
        analysis_prompt_version=f"prompt-{app_id}",
        fastgpt_app_id=app_id,
        model_version="model-v1",
        output_schema_version="schema-v1",
        validator_version="validator-v1",
        memory_resolver_policy_version="memory-v1",
    )


def manifest(layer: str, dataset_id: UUID) -> EvaluationDatasetManifest:
    case = frozen_case(CASE_A).model_copy(
        update={
            "layer": layer,
            "severity": "critical" if layer == "golden" else "high",
        }
    )
    return EvaluationDatasetManifest(
        dataset_id=dataset_id,
        tenant_id="tenant-1",
        dataset_name=f"{layer}-cases",
        dataset_version="v1",
        dataset_layer=layer,
        description="已审批的不可变回放数据",
        source_cutoff_at=datetime(2026, 9, 8, 23, tzinfo=timezone.utc),
        frozen_at=NOW,
        frozen_by="operator-1",
        cases=(case,),
    )


class Runner:
    def __init__(self, driver: str) -> None:
        self.driver = driver

    async def run(self, analysis_input):
        evidence_id = analysis_input.related_news[0].news_id
        report = HotNewsAnalysisReport(
            news_id=analysis_input.news_id,
            trend_assessment="仅依据可信输入得出结论。",
            dominant_driver=self.driver,
            attention_reasons=[
                AnalysisReason(
                    reason_type="metric",
                    statement="点击指标支持判断。",
                    metric_keys=["clicks"],
                    confidence=0.9,
                    certainty="observed",
                ),
                AnalysisReason(
                    reason_type="evidence",
                    statement="白名单历史新闻提供背景。",
                    evidence_news_ids=[evidence_id],
                    confidence=0.9,
                    certainty="observed",
                ),
            ],
            evidence_news_ids=[evidence_id],
            applied_memory_ids=[],
            overall_confidence=0.9,
        )
        return AgentResult(
            value=report,
            request_id="eval-1",
            usage={},
            raw_content=report.model_dump_json(),
        )


class Factory:
    def ensure_supported(self, bundle_spec):
        pass

    def ensure_transition_supported(self, *, baseline, candidate):
        pass

    def create(self, bundle_spec):
        return Runner("growth" if bundle_spec.fastgpt_app_id == "candidate" else "click")


@pytest.mark.asyncio
async def test_offline_replay_compares_three_layers_on_identical_cases() -> None:
    manifests = (
        manifest("golden", UUID("20000000-0000-4000-8000-000000000001")),
        manifest(
            "fresh_bad_case",
            UUID("20000000-0000-4000-8000-000000000002"),
        ),
        manifest(
            "high_risk_regression",
            UUID("20000000-0000-4000-8000-000000000003"),
        ),
    )

    result = await HotNewsOfflineReplayService(Factory()).evaluate(
        manifests=manifests,
        candidate_spec=spec("candidate"),
        online_baseline_spec=spec("baseline"),
        previous_experiment_spec=spec("previous"),
    )

    assert result.suite_metrics.golden.candidate.pass_rate == 0
    assert result.suite_metrics.golden.online_baseline.pass_rate == 1
    assert result.suite_metrics.golden.previous_experiment.pass_rate == 1
    assert result.suite_metrics.golden.candidate.critical_failures == 1
    assert result.suite_metrics.fresh_bad_case.candidate.critical_failures == 0
    serialized = str(result.artifact_payload)
    assert "raw_content" not in serialized
    assert "error_message" not in serialized
