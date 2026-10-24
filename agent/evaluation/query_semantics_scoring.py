"""Offline, fixed-denominator query-slot scoring; never calls a model or a warehouse.

Model proposals are scored before the server's execution gate. Execution
decisions are a separate measure and cannot repair a wrong semantic proposal.
Draft labels remain exploratory, and no score authorizes a production update.
"""

from __future__ import annotations

from pathlib import Path
import json

from pydantic import ValidationError

from app.modules.conversation.query_understanding import QueryUnderstanding
from evaluation.query_semantics_common import load_package, read_json, write_json, sha256_file


SCORER_VERSION = "query-semantics-slots-v1"
SLOTS = (
    "status", "time_expression", "explicit_date", "hour_start", "hour_end",
    "time_basis", "ranking_source", "response_style", "intent.sort_by",
    "intent.sort_direction", "intent.content_type", "intent.category", "intent.row_limit",
)
IDENTITY_FIELDS = (
    "base_model", "base_revision", "tokenizer_revision", "chat_template_sha256", "quantization",
)


def _rate(count: int, denominator: int) -> dict:
    return {"count": count, "denominator": denominator,
            "rate": count / denominator if denominator else None}


def _slots(value: dict) -> dict:
    normalized = QueryUnderstanding.model_validate(value).model_dump(mode="json")
    intent = normalized.get("intent") or {}
    return {key: intent.get(key.split(".")[1]) if key.startswith("intent.")
            else normalized[key] for key in SLOTS}


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as source:
        rows = [json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
                for line in source if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("predictions must be JSON objects")
    return rows


def _summarize(cases: list[dict]) -> dict:
    main = [row for row in cases if row["main_em"]]
    negative = [row for row in cases if row["negative"]]
    execution_negative = [row for row in cases if row["execution_negative"]]
    return {
        "planned": len(cases),
        "completed": sum(row["execution_status"] == "ok" for row in cases),
        "execution_errors": sum(row["execution_status"] == "error" for row in cases),
        "not_run": sum(row["execution_status"] == "not_run" for row in cases),
        "input_blocked": sum(row["execution_status"] == "input_blocked" for row in cases),
        "schema_valid": _rate(sum(row["schema_valid"] for row in cases), len(cases)),
        "query_semantic_exact_match": _rate(sum(row["exact_match"] for row in main), len(main)),
        "slot_accuracy": {slot: _rate(sum(row["slots"].get(slot, False) for row in main), len(main))
                          for slot in SLOTS},
        "negative_decision_accuracy": _rate(sum(row["proposal_decision_correct"] for row in negative), len(negative)),
        "unsafe_ready": _rate(sum(row["unsafe_ready"] for row in negative), len(negative)),
        "resolved_decision_accuracy": _rate(sum(row["resolved_decision_correct"] for row in cases), len(cases)),
        "unsafe_resolved_ready": _rate(sum(row["unsafe_resolved_ready"] for row in execution_negative), len(execution_negative)),
    }


def _blocked(reason: str) -> dict:
    return {"schema_version": 1, "scorer_version": SCORER_VERSION,
            "status": "blocked", "quality_status": "blocked", "production_acceptance": "not_approved",
            "blocking_reasons": [reason], "metrics": None, "cases": [],
            "resume_eligible": False, "resume_em_percent": None}


def _score(package: Path, run: Path) -> dict:
    try:
        manifest, inputs, gold = load_package(package, with_gold=True)
        spec = read_json(run / "run-spec.json")
        completion = read_json(run / "completion.json")
        predictions = _rows(run / "predictions.jsonl")
        if not isinstance(spec, dict) or not isinstance(completion, dict):
            raise ValueError("run artifacts must be objects")
        if completion.get("stop_reason") is not None and not isinstance(completion["stop_reason"], str):
            raise ValueError("stop_reason must be a string or null")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _blocked(f"unreadable_or_invalid_artifact:{type(exc).__name__}")

    reasons: list[str] = []
    if spec.get("package_manifest_sha256") != sha256_file(package / "manifest.json"):
        reasons.append("package_manifest_hash_mismatch")
    if completion.get("predictions_sha256") != sha256_file(run / "predictions.jsonl"):
        reasons.append("predictions_hash_mismatch")
    if not spec.get("source_files_sha256") or spec.get("source_files_sha256") != manifest.get("source_files_sha256"):
        reasons.append("run_source_hash_mismatch")
    input_ids = [row.get("id") for row in inputs]
    gold_ids = [row.get("id") for row in gold]
    prediction_ids = [row.get("id") for row in predictions]
    for kind, ids in (("input", input_ids), ("gold", gold_ids), ("prediction", prediction_ids)):
        if not all(isinstance(value, str) and value for value in ids):
            reasons.append(f"invalid_{kind}_id")
        elif len(ids) != len(set(ids)):
            reasons.append(f"duplicate_{kind}_id")
    # Invalid/duplicate/missing records are evidence loss, not scored model errors.
    if reasons and any(reason.startswith("invalid_") for reason in reasons):
        result = _blocked(reasons[0])
        result.update(blocking_reasons=reasons, planned=len(inputs))
        return result
    if set(input_ids) != set(gold_ids):
        reasons.append("gold_coverage_mismatch")
    if set(input_ids) != set(prediction_ids):
        reasons.append("prediction_coverage_mismatch")
    if not inputs:
        reasons.append("empty_dataset")
    if reasons:
        result = _blocked(reasons[0])
        result.update(blocking_reasons=reasons, planned=len(inputs))
        return result

    by_gold = {row["id"]: row for row in gold}
    by_prediction = {row["id"]: row for row in predictions}
    approved = manifest.get("review_status") == "approved"
    cases = []
    try:
        for item in inputs:
            label, prediction = by_gold[item["id"]], by_prediction[item["id"]]
            expected = _slots(label["target"])
            if not isinstance(label["main_em"], bool):
                raise ValueError("main_em must be boolean")
            if label["main_em"] and expected["status"] != "ready":
                raise ValueError("main_em requires ready target")
            if label["expected_resolution_status"] not in {"ready", "clarify", "unsupported"}:
                raise ValueError("unknown resolution label")
            review = label.get("human_review") or {}
            approved = approved and (review.get("status") == "approved"
                                     and bool(str(review.get("annotator") or "").strip())
                                     and bool(str(review.get("reviewer") or "").strip())
                                     and review["annotator"] != review["reviewer"])
            execution_status = prediction.get("status")
            if execution_status not in {"ok", "error", "not_run", "input_blocked"}:
                raise ValueError("unknown prediction status")
            if prediction.get("error_kind") is not None and not isinstance(prediction["error_kind"], str):
                raise ValueError("error_kind must be a string or null")
            attempts = prediction.get("attempts")
            if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
                raise ValueError("invalid attempts")
            if (execution_status in {"not_run", "input_blocked"} and attempts != 0) or (execution_status == "ok" and attempts < 1):
                raise ValueError("inconsistent attempts")
            raw = prediction.get("proposal")
            proposal = raw if isinstance(raw, dict) else {}
            try:
                actual = _slots(proposal) if execution_status == "ok" else None
            except (ValidationError, ValueError, TypeError):
                actual = None
            actual_slots = {key: actual is not None and actual[key] == expected[key] for key in SLOTS}
            resolution = prediction.get("resolved") or {}
            resolution = resolution if isinstance(resolution, dict) else {}
            resolved_status = resolution.get("status") if execution_status == "ok" else None
            negative = expected["status"] != "ready"
            execution_negative = label["expected_resolution_status"] != "ready"
            cases.append({
                "id": item["id"], "group_id": item["group_id"], "split": item["split"],
                "cohort": item["cohort"], "main_em": label["main_em"],
                "negative": negative, "execution_negative": execution_negative,
                "execution_status": execution_status, "schema_valid": actual is not None,
                "exact_match": all(actual_slots.values()), "slots": actual_slots,
                "failed_slots": [key for key in SLOTS if not actual_slots[key]] if label["main_em"] else [],
                "proposal_decision_correct": actual is not None and actual["status"] == expected["status"],
                "unsafe_ready": negative and proposal.get("status") == "ready",
                "resolved_decision_correct": resolved_status == label["expected_resolution_status"],
                "unsafe_resolved_ready": execution_negative and resolved_status == "ready",
                "error_kind": prediction.get("error_kind"), "elapsed_ms": prediction.get("elapsed_ms"),
                "attempts": attempts,
            })
    except (ValueError, TypeError, KeyError) as exc:
        return _blocked(f"invalid_scoring_record:{type(exc).__name__}")

    identity = spec.get("identity") or {}
    identity_ok = isinstance(identity, dict) and all(identity.get(key) is not None and identity.get(key) != "" for key in IDENTITY_FIELDS)
    if not identity_ok:
        reasons.append("incomplete_model_identity")
    calls = completion.get("real_inference_calls")
    calls_valid = isinstance(calls, int) and not isinstance(calls, bool) and calls == sum(row["attempts"] for row in cases) and calls > 0
    if not calls_valid:
        reasons.append("real_inference_not_evidenced")
    if not approved:
        reasons.append("labels_not_independently_approved")
    if any(row["execution_status"] == "not_run" for row in cases):
        reasons.append("incomplete_run")
    if any(row["execution_status"] == "input_blocked" for row in cases):
        reasons.append("inputs_blocked_before_model")
    identity_failures = {"model_version_mismatch", "unapproved_model", "runner_error"}
    if completion.get("stop_reason") in identity_failures or any(
        row["error_kind"] in identity_failures for row in cases
    ):
        reasons.append("model_identity_or_runner_failure")
    metrics = _summarize(cases)
    heldout = manifest.get("split") in {"validation", "test"} and all(row["split"] == manifest["split"] for row in cases)
    groups = sorted({row["group_id"] for row in cases})
    train_groups = identity.get("training_group_ids") if isinstance(identity, dict) else None
    if isinstance(train_groups, list) and all(isinstance(value, str) for value in train_groups):
        if set(groups) & set(train_groups):
            reasons.append("training_evaluation_group_overlap")
    else:
        reasons.append("invalid_training_group_ledger")
    resume_eligible = not reasons and heldout and metrics["query_semantic_exact_match"]["denominator"] > 0
    rate = metrics["query_semantic_exact_match"]["rate"]
    return {
        "schema_version": 1, "scorer_version": SCORER_VERSION,
        "status": "quality_measured" if not reasons else "exploratory",
        "quality_status": "quality_measured" if not reasons else "blocked",
        "production_acceptance": "not_approved", "blocking_reasons": reasons,
        "dataset_id": manifest["dataset_id"], "split": manifest.get("split"),
        "package_manifest_sha256": spec.get("package_manifest_sha256"),
        "predictions_sha256": sha256_file(run / "predictions.jsonl"),
        "identity": identity, "real_inference_calls": calls,
        "independent_group_count": len(groups), "evaluation_group_ids": groups,
        "metrics": metrics,
        "by_cohort": {name: _summarize([row for row in cases if row["cohort"] == name])
                      for name in sorted({row["cohort"] for row in cases})},
        "cases": cases, "resume_eligible": resume_eligible,
        "resume_em_percent": 100 * rate if resume_eligible else None,
        "limitations": ["Query parsing on the specified frozen set only; no retrieval, SQL execution, production approval or online business effect.",
                        "Slot scores use all preregistered ready main cases; errors and not_run are retained in that denominator.",
                        "No statistical confidence interval or production business threshold is implemented."],
    }


def _save(out: Path, report: dict) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "results.json", report)
    return report


def score_run(package: Path, run: Path, out: Path) -> dict:
    """Write a new score directory, retaining draft metrics as exploratory only."""
    return _save(Path(out), _score(Path(package), Path(run)))


def compare_runs(package: Path, baseline: Path, candidate: Path, out: Path) -> dict:
    """Compare base vs LoRA without editing either original run directory."""
    package, baseline, candidate = Path(package), Path(baseline), Path(candidate)
    base, tuned = _score(package, baseline), _score(package, candidate)
    reasons = []
    try:
        left, right = read_json(baseline / "run-spec.json"), read_json(candidate / "run-spec.json")
        a, b = left.get("identity") or {}, right.get("identity") or {}
        if not isinstance(a, dict) or not isinstance(b, dict):
            raise ValueError("model identities must be objects")
        for key in IDENTITY_FIELDS:
            if a.get(key) is None or a.get(key) == "" or a.get(key) != b.get(key):
                reasons.append(f"identity_mismatch:{key}")
        for key in ("package_manifest_sha256", "source_files_sha256", "dependency_versions", "budget"):
            if not left.get(key) or left.get(key) != right.get(key):
                reasons.append(f"run_contract_mismatch:{key}")
        settings = []
        for spec in (left, right):
            values = spec.get("endpoint_settings")
            settings.append({key: value for key, value in values.items()
                             if key not in {"base_url", "model_route", "endpoint_url_sha256", "api_key_env"}}
                            if isinstance(values, dict) else None)
        if not settings[0] or settings[0] != settings[1]:
            reasons.append("run_contract_mismatch:endpoint_settings")
        if a.get("role") != "base" or b.get("role") != "lora":
            reasons.append("roles_must_be_base_and_lora")
        if a.get("adapter_sha256") or not b.get("adapter_sha256"):
            reasons.append("adapter_identity_invalid")
        for label, identity in (("base", a), ("lora", b)):
            groups = identity.get("training_group_ids")
            if not isinstance(groups, list) or not all(isinstance(value, str) and value for value in groups):
                reasons.append(f"missing_training_group_ledger:{label}")
            elif set(groups) & set(base.get("evaluation_group_ids", [])):
                reasons.append(f"training_evaluation_group_overlap:{label}")
        if a.get("training_group_ids"):
            reasons.append("base_has_task_training_groups")
        if not b.get("training_group_ids"):
            reasons.append("lora_training_group_ledger_empty")
    except (OSError, ValueError, TypeError):
        reasons.append("unreadable_run_contract")
    comparable = not reasons
    for label, score in (("baseline", base), ("candidate", tuned)):
        reasons.extend(f"{label}:{reason}" for reason in score.get("blocking_reasons", []))
        if not score.get("resume_eligible"):
            reasons.append(f"{label}:not_eligible_for_resume")
    eligible = not reasons
    base_em = (base.get("metrics") or {}).get("query_semantic_exact_match", {}).get("rate")
    tuned_em = (tuned.get("metrics") or {}).get("query_semantic_exact_match", {}).get("rate")
    delta = 100 * (tuned_em - base_em) if comparable and base_em is not None and tuned_em is not None else None
    return _save(Path(out), {
        "schema_version": 1, "scorer_version": SCORER_VERSION,
        "status": "quality_measured" if eligible else "blocked",
        "production_acceptance": "not_approved", "comparison": "base_vs_lora",
        "blocking_reasons": list(dict.fromkeys(reasons)),
        "baseline": base, "candidate": tuned,
        "exploratory_delta_percentage_points": delta,
        "resume": {"eligible": eligible, "A_percent": 100 * base_em if eligible else None,
                   "B_percent": 100 * tuned_em if eligible else None,
                   "delta_percentage_points": delta if eligible else None},
        "not_evaluated": ["online_bundle_baseline", "previous_experiment_bundle_baseline", "production_acceptance"],
    })
