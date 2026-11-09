"""Synthetic contract checks only: fake generators, no model or database calls."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.sql_assistant.candidate import SqlCandidateConfig, SqlCandidateError, SqlCandidateResult
from app.sql_assistant.planner import compile_query
from evaluation import run_sql_candidates as runner


class Generator:
    def __init__(self, mode="valid"):
        self.mode = mode
        self.calls = []

    def identity(self):
        return {"provider": "test_double", "version": "fixture-v1"}

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        sql = compile_query(kwargs["intent"], kwargs["scenario"])
        if self.mode == "wrong_sort":
            metric = kwargs["intent"].sort_by
            other = "clicks" if metric != "clicks" else "ctr"
            sql = sql.replace(f"ORDER BY {metric}", f"ORDER BY {other}")
        elif self.mode == "unsafe":
            sql = "SELECT * FROM private.users"
        elif self.mode == "failure":
            raise SqlCandidateError("rate_limit", metadata={
                "usage": {"prompt_tokens": 13}, "attempts": 1, "private_detail": "DO_NOT_EXPORT"})
        elif self.mode == "exception":
            raise RuntimeError("DO_NOT_EXPORT")
        elif self.mode == "timeout":
            raise TimeoutError()
        elif self.mode == "unsupported":
            return SqlCandidateResult("unsupported", None, "unsupported_contract", {})
        elif self.mode == "cancel":
            raise asyncio.CancelledError()
        return SqlCandidateResult("proposed", sql, None, {
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "attempts": 1, "model_request_id": "fixture", "private_detail": "DO_NOT_EXPORT",
        })


@pytest.mark.asyncio
async def test_native_has_no_model_or_database_and_preserves_development_status(monkeypatch):
    from app.db.session import Database
    monkeypatch.setattr(Database, "__init__", lambda *a, **k: pytest.fail("must not open warehouse"))
    report = await runner.evaluate_candidates()
    assert report["summary"]["passed"] == report["summary"]["total"] == 6
    assert report["summary"]["generation_calls"] == report["summary"]["database_executions"] == 0
    assert report["scope"] == "contract_only_no_database"
    assert report["data_status"] == "synthetic_development_not_human_reviewed"
    assert all(row["candidate_sql"] is None and row["attempts"] == 0 for row in report["cases"])


@pytest.mark.asyncio
async def test_valid_proposals_have_full_denominator_usage_and_no_bound_values_in_generator():
    generator = Generator()
    report = await runner.evaluate_candidates(
        config=SqlCandidateConfig(mode="dbgpt_candidate"), generator=generator,
    )
    assert report["summary"]["contract_pass_rate"] == 1
    assert report["summary"]["generation_calls"] == 6
    assert report["summary"]["usage"]["total_tokens"] == 90
    assert report["summary"]["usage_known_cases"] == 6
    assert all(row["database_executions"] == 0 for row in report["cases"])
    assert all("params" not in call and "window_start" not in call for call in generator.calls)
    assert "DO_NOT_EXPORT" not in json.dumps(report)
    assert all(call["schema_version"] == "news-warehouse-v1" for call in generator.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,reason", [
    ("wrong_sort", "sql_contract_rejected"), ("unsafe", "sql_contract_rejected"),
    ("failure", "rate_limit"), ("exception", "candidate_internal_error"),
    ("timeout", "deadline_exceeded"), ("unsupported", "unsupported_contract"),
])
async def test_failures_never_disappear_from_denominator_or_leak_exception(mode, reason):
    report = await runner.evaluate_candidates(
        config=SqlCandidateConfig(mode="dbgpt_shadow"), generator=Generator(mode),
    )
    assert report["summary"]["total"] == report["summary"]["not_passed"] == 6
    assert report["summary"]["contract_pass_rate"] == 0
    assert all(row["reason_code"] == reason for row in report["cases"])
    assert "DO_NOT_EXPORT" not in json.dumps(report)
    if mode == "failure":
        assert report["summary"]["usage"]["prompt_tokens"] == 78


@pytest.mark.asyncio
async def test_call_budget_keeps_unattempted_cases():
    generator = Generator()
    report = await runner.evaluate_candidates(
        config=SqlCandidateConfig(mode="dbgpt_candidate"), generator=generator, max_calls=1,
    )
    assert len(generator.calls) == 1
    assert report["summary"]["total"] == 6
    assert report["summary"]["passed"] == 1
    assert report["summary"]["not_passed"] == report["summary"]["not_run"] == 5
    assert report["summary"]["failed"] == 0


@pytest.mark.asyncio
async def test_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        await runner.evaluate_candidates(
            config=SqlCandidateConfig(mode="dbgpt_candidate"), generator=Generator("cancel"),
        )


@pytest.mark.asyncio
async def test_mode_must_match_generator_presence():
    with pytest.raises(ValueError, match="explicit generator"):
        await runner.evaluate_candidates(config=SqlCandidateConfig(mode="dbgpt_candidate"))
    with pytest.raises(ValueError, match="native must not"):
        await runner.evaluate_candidates(generator=Generator())


@pytest.mark.asyncio
async def test_new_output_only_and_same_contract_comparison(tmp_path):
    baseline = await runner.evaluate_candidates()
    candidate = await runner.evaluate_candidates(
        config=SqlCandidateConfig(mode="dbgpt_candidate"), generator=Generator("wrong_sort"),
    )
    runner.save_report(baseline, tmp_path / "native")
    runner.save_report(candidate, tmp_path / "candidate")
    with pytest.raises(FileExistsError):
        runner.save_report(candidate, tmp_path / "candidate")
    compared = runner.compare_reports(tmp_path / "native", tmp_path / "candidate")
    assert compared["status"] == "comparable"
    assert compared["denominator"] == 6
    assert compared["contract_pass_rate_delta"] == -1
    assert "database_result_equivalence" in compared["not_evaluated"]
    assert compared["production_acceptance"] == "not_approved"


@pytest.mark.asyncio
async def test_comparison_rejects_contract_drift_base_lora_or_tampered_output(tmp_path):
    report = await runner.evaluate_candidates()
    runner.save_report(report, tmp_path / "base")
    changed = deepcopy(report)
    changed["contract"]["schema"]["version"] = "different"
    changed["contract_sha256"] = runner.digest(changed["contract"])
    runner.save_report(changed, tmp_path / "drift")
    with pytest.raises(ValueError, match="identical cases"):
        runner.compare_reports(tmp_path / "base", tmp_path / "drift")
    changed = deepcopy(report)
    changed["experiment_kind"] = "base_vs_lora"
    runner.save_report(changed, tmp_path / "lora")
    with pytest.raises(ValueError, match="identity"):
        runner.compare_reports(tmp_path / "base", tmp_path / "lora")
    with (tmp_path / "base/report.json").open("a") as handle:
        handle.write(" ")
    with pytest.raises(ValueError, match="identity"):
        runner.compare_reports(tmp_path / "base", tmp_path / "base")


def test_cli_native_never_loads_model_and_refuses_dotenv_or_existing_output(tmp_path, monkeypatch, capsys):
    from app.model_runtime import config_file
    monkeypatch.setattr(config_file, "load_model_runtime_config",
                        lambda *a: pytest.fail("native does not load model config"))
    out = tmp_path / "native"
    assert runner.main(["run", "--out", str(out)]) == 0
    assert (out / "report.json").exists()
    assert runner.main(["run", "--out", str(out)]) == 2
    assert runner.main(["run", "--out", str(tmp_path / "blocked"),
                        "--candidate-config", ".env"]) == 2
    assert not (tmp_path / "blocked").exists()


def test_candidate_cli_requires_explicit_model_and_tenant_before_network(tmp_path):
    config = tmp_path / "candidate.yml"
    config.write_text("mode: dbgpt_candidate\n", encoding="utf-8")
    assert runner.main(["run", "--out", str(tmp_path / "out"),
                        "--candidate-config", str(config)]) == 2
    assert not (tmp_path / "out").exists()


@pytest.mark.asyncio
async def test_comparison_rejects_different_candidate_budget(tmp_path):
    baseline = await runner.evaluate_candidates()
    candidate = await runner.evaluate_candidates(
        config=SqlCandidateConfig(mode="dbgpt_candidate", max_output_tokens=4096),
        generator=Generator(),
    )
    runner.save_report(baseline, tmp_path / "baseline")
    runner.save_report(candidate, tmp_path / "candidate")
    with pytest.raises(ValueError, match="budgets"):
        runner.compare_reports(tmp_path / "baseline", tmp_path / "candidate")
