from pathlib import Path

import pytest

from app.evaluation.fault_drill import (
    FaultDrillRunner,
    load_fault_drill_dataset,
)
from app.observability.hot_news import InMemoryHotNewsRunMetrics


PLAN_PATH = (
    Path(__file__).parents[1]
    / "evaluation"
    / "fault_scenarios"
    / "enterprise_rpc_faults_v1.json"
)


@pytest.mark.asyncio
async def test_fault_plan_exercises_retry_and_blocking_boundaries() -> None:
    report = await FaultDrillRunner().run(
        load_fault_drill_dataset(PLAN_PATH)
    )

    assert report.total_scenarios == 9
    assert report.passed_scenarios == 9
    assert report.pass_rate == 1
    by_id = {item.scenario_id: item for item in report.cases}
    assert by_id["behavior-rpc-timeout"].actual_retryable is True
    assert by_id["behavior-watermark-incomplete"].actual_retryable is False
    assert by_id["model-hallucinated-evidence"].metric_recorded is True


def test_hot_news_metrics_use_only_low_cardinality_labels() -> None:
    metrics = InMemoryHotNewsRunMetrics()
    metrics.record(
        "failed",
        error_type="EnterpriseRpcTimeoutError",
        retryable=True,
    )

    output = metrics.render_prometheus()

    assert "news_agent_hot_news_runs_total" in output
    assert 'error_type="EnterpriseRpcTimeoutError"' in output
    assert 'retryable="true"' in output
    assert "tenant" not in output
