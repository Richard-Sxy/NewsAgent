from pathlib import Path

import pytest

from app.clients.fastgpt import AgentResult
from app.evaluation.hot_news import (
    HotNewsEvaluationService,
    load_evaluation_dataset,
)
from app.schemas.hot_news import (
    AnalysisReason,
    HotNewsAnalysisReport,
    RelatedNewsContext,
)


DATASET_PATH = (
    Path(__file__).parents[1]
    / "evaluation"
    / "datasets"
    / "hot_news_eval_seed_v1.json"
)


class PassingEvaluationRunner:
    async def run(self, analysis_input):
        if analysis_input.news_id == "eval-low-signal":
            driver = "insufficient_data"
            evidence_ids = []
            metric_keys = []
            limitations = ["当前样本量和关联证据不足。"]
        elif analysis_input.news_id == "eval-ai-current":
            driver = "click"
            evidence_ids = ["eval-ai-history"]
            metric_keys = ["clicks", "growth_component"]
            limitations = []
        else:
            driver = "click"
            evidence_ids = ["eval-huawei-history"]
            metric_keys = ["ctr"]
            limitations = []

        reasons = []
        if metric_keys:
            reasons.append(
                AnalysisReason(
                    reason_type="metric",
                    statement="权威指标支持当前热度判断。",
                    metric_keys=metric_keys,
                    evidence_news_ids=[],
                    confidence=0.9,
                    certainty="observed",
                )
            )
        if evidence_ids:
            reasons.append(
                AnalysisReason(
                    reason_type="evidence",
                    statement="同主体历史报道提供事件背景。",
                    metric_keys=[],
                    evidence_news_ids=evidence_ids,
                    confidence=0.8,
                    certainty="observed",
                )
            )
        report = HotNewsAnalysisReport(
            news_id=analysis_input.news_id,
            trend_assessment="基于输入指标和白名单证据形成判断。",
            dominant_driver=driver,
            attention_reasons=reasons,
            related_contexts=(
                [
                    RelatedNewsContext(
                        statement="已有同事件历史背景。",
                        evidence_news_ids=evidence_ids,
                        confidence=0.8,
                    )
                ]
                if evidence_ids
                else []
            ),
            operation_suggestions=[],
            evidence_news_ids=evidence_ids,
            applied_memory_ids=[],
            limitations=limitations,
            overall_confidence=0.8,
        )
        return AgentResult(
            value=report,
            request_id=f"eval-{analysis_input.news_id}",
            usage={"total_tokens": 10},
            raw_content=report.model_dump_json(),
        )


def test_seed_evaluation_dataset_is_strict_and_versioned() -> None:
    dataset = load_evaluation_dataset(DATASET_PATH)

    assert len(dataset.cases) == 3
    assert len(dataset.content_sha256) == 64
    assert {case.case_id for case in dataset.cases} == {
        "ai-datacenter-growth-with-evidence",
        "low-signal-without-evidence",
        "company-identity-conflict",
    }


@pytest.mark.asyncio
async def test_evaluation_service_scores_contract_and_business_labels() -> None:
    report = await HotNewsEvaluationService(
        PassingEvaluationRunner()
    ).evaluate(load_evaluation_dataset(DATASET_PATH))

    assert report.total_cases == 3
    assert report.passed_cases == 3
    assert report.pass_rate == 1
    assert report.contract_pass_rate == 1
    assert report.mean_required_evidence_recall == 1
    assert report.mean_required_metric_coverage == 1
