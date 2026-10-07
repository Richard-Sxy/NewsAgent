"""Opt-in large fixtures through real SQL, Workflow and isolated analysis."""

from decimal import Decimal
import os

import pytest

from examples.data_analysis_demo import list_demo_scenarios, prepare_demo


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scaled_catalog_top100_keeps_all_ten_cases_and_six_operations():
    base_url = os.getenv("NEWSAGENT_SCALED_DEMO_BASE_URL")
    if not base_url:
        pytest.skip("set NEWSAGENT_SCALED_DEMO_BASE_URL to the separate scaled synthetic API")
    catalog = await list_demo_scenarios(base_url=base_url)
    dataset = catalog["dataset"]
    assert dataset["dataset_profile"] == "enterprise-v2"
    assert dataset["news_per_tenant"] in (120, 1200, 12000)
    assert dataset["total_news_count"] == dataset["news_per_tenant"] * 2
    assert dataset["total_metric_row_count"] == dataset["total_baseline_row_count"] == dataset["total_news_count"] * 24
    reports = {}
    for scenario in catalog["scenarios"]:
        report = await prepare_demo(base_url=base_url, scenario_id=scenario["id"])
        assert report["status"] == "verified" and report["dataset"] == dataset
        assert report["current_row_count"] == report["reference_row_count"] == 100
        assert report["current_run_id"] != report["reference_run_id"]
        assert {item["operation"] for item in report["operations"]} == {
            "overview", "distribution", "compare", "quality", "baseline", "trend",
        }
        for item in report["operations"]:
            assert item["execution"]["transport"] == "service"
            assert item["execution"]["os_resource_limits_enforced"] is True
        reports[scenario["id"]] = {item["operation"]: item["analysis"] for item in report["operations"]}
        print(f"verified scaled {scenario['id']}: 100+100 real sources, six isolated analysis receipts", flush=True)

    assert len(reports) == 10
    assert Decimal(reports["breaking"]["trend"]["aggregate"]["relative_change"]) > 0
    assert all(Decimal(item["relative_change"]) < 0 for item in reports["fatigue"]["baseline"]["items"])
    funnel = reports["funnel"]["overview"]["totals"]
    assert Decimal(funnel["effective_consumptions"]) / Decimal(funnel["clicks"]) <= Decimal("0.2")
    assert Decimal(reports["funnel"]["trend"]["aggregate"]["relative_change"]) < 0
    assert Decimal(reports["content-mix"]["trend"]["aggregate"]["relative_change"]) > 0
    assert len(reports["low-volume"]["quality"]["zero_impression_news_ids"]) == 50
    assert reports["low-volume"]["overview"]["totals"]["impressions"] <= 200
    assert all(item["reference_value"] == "0" and item["relative_change"] is None
               for item in reports["zero-baseline"]["baseline"]["items"])
    churn = reports["ranking-churn"]["trend"]
    shift = 20 if dataset["news_per_tenant"] == 120 else 40
    assert churn["matched_news_count"] == 100 - shift
    assert len(churn["added_news_ids"]) == len(churn["removed_news_ids"]) == shift
    assert churn["aggregate"]["current_value"] == churn["aggregate"]["reference_value"] == "0.125"
    assert reports["recovery-gap"]["trend"]["gap_seconds"] == 3600
    assert reports["precision"]["overview"]["totals"]["impressions"] > 2**53 - 1
    assert all("unique_users" not in report["overview"]["totals"] for report in reports.values())
