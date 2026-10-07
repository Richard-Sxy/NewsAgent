"""Synthetic scenario expectations must traverse real bounded worker execution."""

import json
import sys

import pytest

from examples.enterprise_analysis_contracts import CONTRACT_VERSION, list_scenarios, main, run_scenarios


@pytest.mark.asyncio
async def test_all_contract_cases_run_real_workers_or_real_preflight_rejections():
    report = await run_scenarios()
    assert report["contract_version"] == CONTRACT_VERSION
    assert report["status"] == "verified"
    assert report["counts"] == {"total": 20, "passed": 20, "failed": 0}
    cases = {case["id"]: case for case in report["cases"]}
    for case in cases.values():
        assert case["synthetic"] is True
        assert case["actual"] == case["expected"]
        assert len(case["evidence"]["input_sha256"]) == len(case["evidence"]["output_sha256"]) == 64
        execution = case["execution"]
        if case["actual"]["status"] == "accepted":
            assert execution["worker_attempted"] is True
            assert execution["backend"] == "process"
            assert execution["os_resource_limits_enforced"] == sys.platform.startswith("linux")
            assert len(execution["input_sha256"]) == 64
        else:
            assert execution["worker_attempted"] is False
            assert execution["backend"] is None
            assert execution["os_resource_limits_enforced"] is None
    assert cases["missing-baseline"]["actual"]["checks"]["analysis.items.0.reference_value"] is None
    assert cases["zero-exposure-all"]["actual"]["checks"]["analysis.weighted_ctr"] is None
    assert cases["zero-clicks-with-exposure"]["actual"]["checks"]["analysis.aggregate.relative_change"] == "-1"
    assert cases["cohort-turnover"]["actual"]["checks"]["analysis.aggregate.current_value"] == "15"
    assert cases["gap-windows"]["actual"]["checks"]["analysis.gap_seconds"] == 7200
    assert cases["ctr-mismatch"]["aggregate_port_contract"]["actual"] == {
        "status": "contract_failed", "error_code": "aggregate_ctr_inconsistent",
    }
    assert cases["repeated-clicks"]["aggregate_port_contract"]["actual"]["status"] == "local_contract_passed"
    assert cases["repeated-clicks"]["aggregate_port_contract"]["tenant_isolation"] == "not_verified"
    serialized = json.dumps(report, ensure_ascii=False)
    assert "untrusted-unknown-field-sentinel" not in serialized
    assert "raw_event" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [(), ("missing-baseline", "missing-baseline"), ("unknown",)])
async def test_unapproved_or_duplicate_scenarios_are_rejected(selection):
    with pytest.raises(ValueError, match="scenario_selection_invalid"):
        await run_scenarios(scenario_ids=selection)


def test_cli_list_and_single_case_match_public_interface(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["enterprise_analysis_contracts", "--list"])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["scenarios"] == list_scenarios()
    assert len(report["scenarios"]) == 20
    monkeypatch.setattr(sys, "argv", ["enterprise_analysis_contracts", "--scenario", "rounded-ctr"])
    main()
    case_report = json.loads(capsys.readouterr().out)
    assert case_report["status"] == "verified"
    assert case_report["counts"] == {"total": 1, "passed": 1, "failed": 0}
    assert case_report["cases"][0]["id"] == "rounded-ctr"
    assert case_report["cases"][0]["execution"]["worker_attempted"] is True


@pytest.mark.parametrize("selection", [[], ["--all"]])
def test_cli_default_and_all_execute_every_approved_scenario(selection, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["enterprise_analysis_contracts", *selection])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "verified"
    assert report["counts"] == {"total": 20, "passed": 20, "failed": 0}
    assert {case["id"] for case in report["cases"]} == {case["id"] for case in list_scenarios()}
