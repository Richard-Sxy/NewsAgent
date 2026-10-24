from copy import deepcopy
import hashlib
import json

import pytest

from evaluation import query_semantics_scoring as scoring


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def predictions(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    write(path.parent / "completion.json", {
        "real_inference_calls": sum(row["attempts"] for row in rows),
        "predictions_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    })


@pytest.fixture
def evaluation(tmp_path, monkeypatch):
    target = {
        "status": "ready", "time_expression": "date", "explicit_date": "2026-10-09",
        "hour_start": 19, "hour_end": 20, "time_basis": "behavior",
        "ranking_source": "unspecified", "response_style": "list", "unsupported_conditions": [],
        "intent": {"sort_by": "ctr", "sort_direction": "desc", "content_type": "video",
                   "category": "财经", "row_limit": 5, "explanation": "查询点击率"},
    }
    inputs = [{"id": "positive", "group_id": "heldout-1", "split": "test", "cohort": "golden"},
              {"id": "negative", "group_id": "heldout-2", "split": "test", "cohort": "high_risk"}]
    negative = deepcopy(target)
    negative.update(status="clarify", intent=None, unsupported_conditions=["ambiguous"])
    gold = [{"id": case["id"], "target": deepcopy(wanted), "expected_resolution_status": wanted["status"],
             "main_em": wanted["status"] == "ready",
             "human_review": {"status": "approved", "annotator": "human-1", "reviewer": "human-2"}}
            for case, wanted in zip(inputs, (target, negative))]
    manifest = {"schema_version": 1, "dataset_id": "unit-fixture-v1", "split": "test", "review_status": "approved",
                "source_files_sha256": {"scorer.py": "c" * 64}}
    package = tmp_path / "package"
    package.mkdir()
    write(package / "manifest.json", manifest)
    monkeypatch.setattr(scoring, "load_package", lambda path, with_gold: (manifest, deepcopy(inputs), deepcopy(gold)))
    rows = [{"id": case["id"], "status": "ok", "proposal": deepcopy(wanted), "resolved": {"status": wanted["status"]},
             "attempts": 1, "elapsed_ms": 10, "error_kind": None}
            for case, wanted in zip(inputs, (target, negative))]
    base, lora = tmp_path / "base", tmp_path / "lora"
    for folder, role in ((base, "base"), (lora, "lora")):
        folder.mkdir()
        identity = {"role": role, "base_model": "Hunyuan-7B-Instruct", "base_revision": "model-revision",
                    "tokenizer_revision": "tokenizer-revision", "chat_template_sha256": "a" * 64,
                    "quantization": "bf16", "adapter_sha256": "b" * 64 if role == "lora" else None,
                    "training_group_ids": ["train-1"] if role == "lora" else []}
        write(folder / "run-spec.json", {"package_manifest_sha256": scoring.sha256_file(package / "manifest.json"),
              "identity": identity, "endpoint_settings": {"base_url": "http://localhost:8000", "model_route": role,
              "endpoint_url_sha256": "a" * 64 if role == "base" else "b" * 64,
              "api_key_env": "BASE_KEY" if role == "base" else "LORA_KEY",
              "timeout": 60, "response_format": "json_object", "extra_body": {"temperature": 0}},
              "source_files_sha256": {"scorer.py": "c" * 64}, "dependency_versions": {"pydantic": "2.test"},
              "budget": {"max_calls": 2, "max_seconds": 60, "concurrency": 1, "attempts_per_case": 1}})
        predictions(folder / "predictions.jsonl", rows)
    return {"package": package, "base": base, "lora": lora, "gold": gold, "manifest": manifest,
            "rows": rows, "tmp": tmp_path, "inputs": inputs}


def run_score(data):
    return scoring.score_run(data["package"], data["base"], data["tmp"] / "score")


def test_main_exact_match_excludes_explanation_but_includes_every_semantic_slot(evaluation):
    data = evaluation
    rows = deepcopy(data["rows"])
    rows[0]["proposal"]["intent"]["explanation"] = "不同措辞不应算错"
    predictions(data["base"] / "predictions.jsonl", rows)
    score = run_score(data)
    assert score["metrics"]["query_semantic_exact_match"] == {"count": 1, "denominator": 1, "rate": 1.0}
    assert score["metrics"]["negative_decision_accuracy"]["count"] == 1
    assert score["by_cohort"]["high_risk"]["query_semantic_exact_match"]["rate"] is None
    assert score["production_acceptance"] == "not_approved"


@pytest.mark.parametrize("field,value", [("response_style", "summary"), ("hour_start", 18),
                                          ("time_basis", "publication"), ("ranking_source", "enterprise_board")])
def test_slot_error_not_repaired_by_successful_execution_gate(evaluation, field, value):
    data = evaluation
    rows = deepcopy(data["rows"])
    rows[0]["proposal"][field] = value
    predictions(data["base"] / "predictions.jsonl", rows)
    score = run_score(data)
    assert score["metrics"]["query_semantic_exact_match"]["count"] == 0
    assert score["metrics"]["slot_accuracy"][field]["count"] == 0
    assert score["metrics"]["resolved_decision_accuracy"]["count"] == 2


def test_execution_error_stays_in_denominator(evaluation):
    data = evaluation
    rows = deepcopy(data["rows"])
    rows[0].update(status="error", proposal=None, resolved=None, error_kind="timeout")
    predictions(data["base"] / "predictions.jsonl", rows)
    score = run_score(data)
    assert score["metrics"]["execution_errors"] == 1
    assert score["metrics"]["query_semantic_exact_match"] == {"count": 0, "denominator": 1, "rate": 0.0}
    assert score["quality_status"] == "quality_measured"


def test_not_run_blocks_quality_without_shrinking_denominator(evaluation):
    data = evaluation
    rows = deepcopy(data["rows"])
    rows[0].update(status="not_run", proposal=None, resolved=None, attempts=0)
    predictions(data["base"] / "predictions.jsonl", rows)
    score = run_score(data)
    assert score["metrics"]["query_semantic_exact_match"]["denominator"] == 1
    assert score["quality_status"] == "blocked"
    assert "incomplete_run" in score["blocking_reasons"]
    assert score["resume_em_percent"] is None


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra", "invalid_id", "tamper"])
def test_prediction_integrity_blocks_all_quality_metrics(evaluation, mutation):
    data = evaluation
    rows = deepcopy(data["rows"])
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif mutation == "extra":
        rows.append({**rows[0], "id": "unplanned"})
    elif mutation == "invalid_id":
        rows[0]["id"] = {"invalid": "id"}
    predictions(data["base"] / "predictions.jsonl", rows)
    if mutation == "tamper":
        with (data["base"] / "predictions.jsonl").open("a") as stream:
            stream.write("\n")
    score = run_score(data)
    assert score["status"] == "blocked"
    assert score["metrics"] is None
    assert score["resume_em_percent"] is None


def test_unsafe_negative_ready_and_resolved_ready_are_separate(evaluation):
    data = evaluation
    rows = deepcopy(data["rows"])
    rows[1]["proposal"] = deepcopy(rows[0]["proposal"])
    rows[1]["resolved"] = {"status": "unsupported"}
    predictions(data["base"] / "predictions.jsonl", rows)
    score = run_score(data)
    assert score["metrics"]["unsafe_ready"]["count"] == 1
    assert score["metrics"]["unsafe_resolved_ready"]["count"] == 0
    assert score["metrics"]["negative_decision_accuracy"]["count"] == 0


def test_schema_invalid_output_is_failed_without_dropping_case(evaluation):
    data = evaluation
    rows = deepcopy(data["rows"])
    rows[0]["proposal"]["unexpected_sql"] = "SELECT 1"
    predictions(data["base"] / "predictions.jsonl", rows)
    score = run_score(data)
    assert score["metrics"]["schema_valid"]["count"] == 1
    assert score["metrics"]["query_semantic_exact_match"] == {"count": 0, "denominator": 1, "rate": 0.0}


def test_input_block_never_becomes_model_success(evaluation):
    data = evaluation
    rows = deepcopy(data["rows"])
    rows[1].update(status="input_blocked", attempts=0)
    predictions(data["base"] / "predictions.jsonl", rows)
    score = run_score(data)
    assert score["metrics"]["negative_decision_accuracy"]["count"] == 0
    assert "inputs_blocked_before_model" in score["blocking_reasons"]
    assert score["resume_em_percent"] is None


def test_inference_count_drift_blocks_quality(evaluation):
    path = evaluation["base"] / "completion.json"
    completion = json.loads(path.read_text())
    completion["real_inference_calls"] = 999
    write(path, completion)
    score = run_score(evaluation)
    assert "real_inference_not_evidenced" in score["blocking_reasons"]
    assert score["resume_em_percent"] is None


@pytest.mark.parametrize("kind", ["model_version_mismatch", "unapproved_model", "runner_error"])
def test_last_case_identity_failure_blocks_quality_even_without_not_run(evaluation, kind):
    data = evaluation
    rows = deepcopy(data["rows"])
    rows[-1].update(status="error", proposal=None, resolved=None, error_kind=kind)
    predictions(data["base"] / "predictions.jsonl", rows)
    score = run_score(data)
    assert score["metrics"]["not_run"] == 0
    assert score["quality_status"] == "blocked"
    assert "model_identity_or_runner_failure" in score["blocking_reasons"]


def test_completion_identity_stop_alone_blocks_quality(evaluation):
    path = evaluation["base"] / "completion.json"
    completion = json.loads(path.read_text())
    completion["stop_reason"] = "model_version_mismatch"
    write(path, completion)
    score = run_score(evaluation)
    assert "model_identity_or_runner_failure" in score["blocking_reasons"]


def test_draft_gold_returns_exploratory_scores_and_null_resume_numbers(evaluation):
    evaluation["gold"][0]["human_review"]["status"] = "draft"
    score = run_score(evaluation)
    assert score["status"] == "exploratory"
    assert score["metrics"]["query_semantic_exact_match"]["rate"] == 1.0
    assert score["resume_em_percent"] is None


def test_base_lora_comparison_allows_different_endpoint_routes_and_no_run_mutation(evaluation):
    data = evaluation
    before = {str(path): path.read_bytes() for folder in (data["base"], data["lora"]) for path in folder.iterdir()}
    report = scoring.compare_runs(data["package"], data["base"], data["lora"], data["tmp"] / "comparison")
    assert report["resume"] == {"eligible": True, "A_percent": 100.0, "B_percent": 100.0, "delta_percentage_points": 0.0}
    assert report["comparison"] == "base_vs_lora"
    assert all(path.read_bytes() == before[str(path)] for folder in (data["base"], data["lora"]) for path in folder.iterdir())


@pytest.mark.parametrize("change", ["training_overlap", "training_missing", "tokenizer", "decoding", "source", "manifest"])
def test_comparison_rejects_incomparable_or_contaminated_runs(evaluation, change):
    data = evaluation
    path = data["lora"] / "run-spec.json"
    spec = json.loads(path.read_text())
    if change == "training_overlap":
        spec["identity"]["training_group_ids"] = ["heldout-1"]
    elif change == "training_missing":
        spec["identity"].pop("training_group_ids")
    elif change == "tokenizer":
        spec["identity"]["tokenizer_revision"] = "other"
    elif change == "decoding":
        spec["endpoint_settings"]["extra_body"]["temperature"] = 1
    elif change == "source":
        spec["source_files_sha256"] = {"scorer.py": "d" * 64}
    else:
        spec["package_manifest_sha256"] = "f" * 64
    write(path, spec)
    report = scoring.compare_runs(data["package"], data["base"], data["lora"], data["tmp"] / "comparison")
    assert report["status"] == "blocked"
    assert report["resume"]["A_percent"] is None
    assert report["resume"]["B_percent"] is None


def test_development_set_cannot_supply_resume_A_B(evaluation):
    data = evaluation
    data["manifest"]["split"] = "development"
    for item in data["inputs"]:
        item["split"] = "development"
    report = scoring.compare_runs(data["package"], data["base"], data["lora"], data["tmp"] / "comparison")
    assert report["baseline"]["quality_status"] == "quality_measured"
    assert report["resume"]["eligible"] is False
    assert report["resume"]["A_percent"] is None


def test_score_output_is_never_overwritten(evaluation):
    run_score(evaluation)
    with pytest.raises(FileExistsError):
        run_score(evaluation)
