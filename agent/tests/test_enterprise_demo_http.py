"""Opt-in operational cases through the actual isolated HTTP/Temporal stack."""

from decimal import Decimal
import os

import pytest

from examples.data_analysis_demo import list_demo_scenarios, prepare_demo


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ten_enterprise_cases_use_real_sources_and_verify_operational_boundaries():
    base_url = os.getenv("NEWSAGENT_ENTERPRISE_DEMO_BASE_URL")
    if not base_url:
        pytest.skip("set NEWSAGENT_ENTERPRISE_DEMO_BASE_URL to the isolated enterprise synthetic API")
    catalog = await list_demo_scenarios(base_url=base_url)
    assert catalog["dataset"]["dataset_profile"] == "enterprise-v1"
    assert len(catalog["scenarios"]) == 10
    reports = {}
    for scenario in catalog["scenarios"]:
        report = await prepare_demo(base_url=base_url, scenario_id=scenario["id"])
        assert report["status"] == "verified" and report["dataset"] == catalog["dataset"]
        assert report["scenario"]["id"] == scenario["id"]
        assert report["current_run_id"] != report["reference_run_id"]
        assert report["current_row_count"] == report["reference_row_count"] == scenario["row_limit"]
        assert {item["operation"] for item in report["operations"]} == {
            "overview", "distribution", "compare", "quality", "baseline", "trend",
        }
        for item in report["operations"]:
            assert item["execution"]["transport"] == "service"
            assert item["execution"]["os_resource_limits_enforced"] is True
        reports[scenario["id"]] = {item["operation"]: item["analysis"] for item in report["operations"]}
        print(f"verified enterprise case {scenario['id']}: six real analysis receipts")

    # These are independently stated business properties, not merely the
    # runner's expected JSON. They cover different downstream boundaries.
    breaking = reports["breaking"]["baseline"]["items"]
    assert sum(Decimal(item["current_value"]) for item in breaking) > sum(
        Decimal(item["reference_value"]) for item in breaking
    )
    assert all(Decimal(item["relative_change"]) < 0 for item in reports["fatigue"]["baseline"]["items"])
    funnel = reports["funnel"]["overview"]["totals"]
    assert Decimal(funnel["effective_consumptions"]) / Decimal(funnel["clicks"]) <= Decimal("0.2")
    assert Decimal(reports["funnel"]["trend"]["aggregate"]["relative_change"]) < 0
    assert Decimal(reports["content-mix"]["trend"]["aggregate"]["relative_change"]) > 0
    assert len(reports["low-volume"]["quality"]["zero_impression_news_ids"]) == 3
    assert reports["low-volume"]["overview"]["totals"]["impressions"] < 100
    assert all(item["reference_value"] == "0" and item["relative_change"] is None
               for item in reports["zero-baseline"]["baseline"]["items"])
    churn = reports["ranking-churn"]["trend"]
    assert churn["matched_news_count"] == 2
    assert len(churn["added_news_ids"]) == len(churn["removed_news_ids"]) == 3
    assert churn["aggregate"]["current_value"] == churn["aggregate"]["reference_value"] == "0.125"
    assert reports["recovery-gap"]["trend"]["gap_seconds"] == 3600
    precision = reports["precision"]["overview"]["totals"]
    assert precision["impressions"] > 2**53 - 1
    assert precision["impressions"] == 12 * (10_000_000_000_000_001 + 55_537) + (97 + 13) * 78
    assert precision["clicks"] == 12 * (1_000_000_000_000_001 + 7_919) + (29 + 7) * 78
    assert all("unique_users" not in report["overview"]["totals"] for report in reports.values())
