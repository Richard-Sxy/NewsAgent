"""Offline contracts for frozen report runs, never real inference."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from evaluation import run_report_pilot as cli


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "source_identity", lambda: {"agent/evaluation/run_report_pilot.py": "fixture-code"})
    monkeypatch.setattr(cli, "scoring_identity", lambda: {"scorer": "fixture-scorer"})
    monkeypatch.setattr(cli, "dependencies", lambda: {"fixture": "1"})
    label_path = tmp_path / "gold.json"
    cli.init_labels(cli.parser().parse_args(["init-labels", "--output-file", str(label_path)]))
    gold = cli.read(label_path)
    gold.update(status="approved", reviewer="unit-test-only", reviewed_at="2026-10-24T12:00:00+08:00")
    label_path.write_text(json.dumps(gold), encoding="utf-8")
    out = tmp_path / "run"
    args = cli.parser().parse_args(["prepare", "--gold-file", str(label_path), "--output-dir", str(out)])
    cli.prepare(args)
    return out, args


def test_prepare_freezes_only_selected_inputs_and_no_private_labels(prepared):
    run, _ = prepared
    spec = cli.verify(run, for_execution=True)
    assert spec["planned"] == 2
    assert spec["budget"]["max_calls"] == 12
    rows = cli.read(run / "inputs.json")
    assert all(row["split"] == "development" for row in rows)
    assert "expected" not in json.dumps(rows)
    assert "source_facts" not in json.dumps(rows)
    assert spec["production_acceptance"] == "not_approved"


def test_unreviewed_gold_blocks_before_creating_run(tmp_path):
    labels = tmp_path / "pending.json"
    cli.init_labels(cli.parser().parse_args(["init-labels", "--output-file", str(labels)]))
    out = tmp_path / "run"
    args = cli.parser().parse_args(["prepare", "--gold-file", str(labels), "--output-dir", str(out)])
    with pytest.raises(ValueError, match="pending human review"):
        cli.prepare(args)
    assert not out.exists()


def test_exploratory_mode_cannot_be_promoted_to_quality_score(tmp_path):
    labels = tmp_path / "pending.json"
    cli.init_labels(cli.parser().parse_args(["init-labels", "--output-file", str(labels)]))
    run = tmp_path / "run"
    cli.prepare(cli.parser().parse_args(["prepare", "--gold-file", str(labels), "--output-dir", str(run), "--exploratory"]))
    with pytest.raises(ValueError, match="exploratory"):
        cli.score(cli.parser().parse_args(["score", "--run-dir", str(run), "--human-review", str(labels)]))


def test_input_drift_and_code_drift_are_blocked(prepared, monkeypatch):
    run, _ = prepared
    monkeypatch.setattr(cli, "source_identity", lambda: {"changed": "code"})
    with pytest.raises(ValueError, match="code changed"):
        cli.verify(run, for_execution=True)
    (run / "inputs.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="inputs.json changed"):
        cli.verify(run)


def test_prepare_refuses_overwrite(prepared):
    run, args = prepared
    before = (run / "run-spec.json").read_bytes()
    with pytest.raises(ValueError, match="exists"):
        cli.prepare(args)
    assert (run / "run-spec.json").read_bytes() == before


def test_private_fields_rejected_even_if_dataset_manifest_is_resealed(tmp_path):
    source = cli.DEFAULT_DATASET
    dest = tmp_path / "dataset"
    (dest / "private").mkdir(parents=True)
    rows = [json.loads(line) for line in (source / "inputs.jsonl").read_text().splitlines()]
    rows[0]["input"]["requirements"]["expected"] = "private answer"
    (dest / "inputs.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (dest / "private/labels.draft.jsonl").write_bytes((source / "private/labels.draft.jsonl").read_bytes())
    manifest = cli.read(source / "manifest.json")
    manifest["files"]["inputs.jsonl"]["sha256"] = cli.digest(dest / "inputs.jsonl")
    cli.save(dest / "manifest.json", manifest)
    with pytest.raises(ValueError, match="unexpected report requirements"):
        cli.load_dataset(dest, "development")


@pytest.mark.asyncio
async def test_interrupted_run_retains_attempts_and_cannot_retry(prepared, monkeypatch):
    from evaluation import report_pilot_core
    run, _ = prepared

    async def interrupted(case, client, output_dir, *, budget):
        output_dir.mkdir(parents=True)
        budget.reserve()
        (output_dir / "attempts.jsonl").write_text(json.dumps({"status": "attempt_started", "stage": "research"}) + "\n")
        raise asyncio.CancelledError()

    monkeypatch.setattr(report_pilot_core, "run_report_case", interrupted)
    result = await cli.execute(run, "", client_override=object())
    assert result["model_attempts"] == 1
    assert result["blocked"] == 1
    assert result["not_run"] == 1
    assert result["backend"] == "injected_test_client"
    summary, traces = cli.read_execution(run, cli.verify(run))
    assert traces[0]["model_calls"] == 1
    assert traces[0]["partial_artifacts_require_audit"] is True
    assert traces[1]["model_calls"] == 0
    with pytest.raises(ValueError, match="already started"):
        await cli.execute(run, "", client_override=object())
    with pytest.raises(ValueError, match="injected"):
        cli.score(cli.parser().parse_args(["score", "--run-dir", str(run),
                                          "--human-review", str(run / "human-review.template.json")]))


def test_changed_trace_blocks_review_template(prepared):
    run, _ = prepared
    spec = cli.verify(run)
    traces = []
    for case in cli.read(run / "inputs.json"):
        path = run / "cases" / case["case_id"]
        path.mkdir(parents=True)
        cli.save(path / "execution.json", {"case_id": case["case_id"]})
        traces.append({"case_id": case["case_id"], "execution_sha256": cli.digest(path / "execution.json")})
    cli.save(run / "execution-summary.json", {"run_id": spec["run_id"], "cases": traces})
    cli.seal(run / "execution-summary.json")
    target = run / "cases" / spec["case_ids"][0] / "execution.json"
    target.write_text("{}")
    with pytest.raises(ValueError, match="trace changed"):
        cli.read_execution(run, spec)


def test_compare_suppresses_delta_when_eligible_error_cases_change(tmp_path, monkeypatch):
    spec = {"run_id": "b", "case_ids": ["a", "b"], "split": "development", "as_of": {},
            "budget": {}, "inference": {}, "frozen_files": {}, "scenes": [], "prompts": [],
            "source_files": {}, "planned": 2}
    def metric(count, denominator):
        return {"reviewer_detection_rate": {"count": count, "denominator": denominator,
                                            "rate": count / denominator if denominator else None}}
    before = {"status": "scored", "metrics": metric(1, 1), "cases": [
        {"case_id": "a", "metrics": metric(1, 1)}, {"case_id": "b", "metrics": metric(0, 0)}]}
    after = {"status": "scored", "metrics": metric(0, 1), "cases": [
        {"case_id": "a", "metrics": metric(0, 0)}, {"case_id": "b", "metrics": metric(0, 1)}]}
    def load(path):
        return spec, before if path.name == "baseline" else after, {"source_files": {}}
    monkeypatch.setattr(cli, "load_scored", load)
    result = cli.compare(cli.parser().parse_args(["compare", "--baseline", str(tmp_path / "baseline"),
        "--candidate", str(tmp_path / "candidate"), "--output-dir", str(tmp_path / "compare")]))
    observation = result["metrics"]["reviewer_detection_rate"]
    assert observation["delta_percentage_points"] is None
    assert observation["same_eligible_cases_and_denominators"] is False


def test_compare_rejects_per_scene_model_change(tmp_path, monkeypatch):
    spec = {"case_ids": [], "split": "development", "as_of": {}, "budget": {},
            "inference": {}, "frozen_files": {}, "scenes": [{"scene": "research", "model_route": "one"}]}
    def load(path):
        item = deepcopy(spec)
        if path.name == "candidate":
            item["scenes"][0]["model_route"] = "two"
        return item, {}, {}
    monkeypatch.setattr(cli, "load_scored", load)
    with pytest.raises(ValueError, match="per-scene model route"):
        cli.compare(cli.parser().parse_args(["compare", "--baseline", str(tmp_path / "baseline"),
            "--candidate", str(tmp_path / "candidate"), "--output-dir", str(tmp_path / "compare")]))
