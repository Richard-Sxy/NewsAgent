"""Read-only integrity verification of a frozen writing run at a new local path.

The original plan and its absolute provenance strings remain untouched. All
dataset reads use the explicitly supplied local root. This module never opens
a model endpoint, changes a run artifact, creates a lock or resumes generation.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
RUNNER_SOURCE = ROOT / "agent/evaluation/run_writing_batch.py"
SPEC = importlib.util.spec_from_file_location("frozen_writing_batch_portable_helpers", RUNNER_SOURCE)
BATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BATCH)
DATASET_FILES = (
    "manifest.json", "inputs.jsonl", "source-manifest.json",
    "private/gold-review.draft.json", "editing-subset.json",
)
FROZEN_FILES = ("inputs.json", "dataset-manifest.json", "source-manifest.json", "editing-subset.json")
IDENTITY_FIELDS = ("case_id", "event_group_id", "arm", "request_id", "request_sha256")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def local_file(root, name):
    """Refuse traversal and symlinks outside the explicit artifact root."""
    root = Path(root).resolve()
    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact_path_escapes_explicit_root") from exc
    require(target.is_file(), "required_artifact_missing:" + name)
    return target


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check_sha(root, name, expected, reason):
    require(isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{64}", expected), "missing_recorded_sha256")
    path = local_file(root, name)
    require(BATCH.sha(path) == expected, reason)
    return path


def _verify_profile(plan):
    require(BATCH.sha(RUNNER_SOURCE) == plan.get("source_sha256"), "runner_source_sha256_mismatch")
    require(plan.get("service_profile") == BATCH.expected_service_profile(),
            "service_or_runtime_source_or_profile_mismatch")
    expected = {
        "schema_version": 1, "mode": BATCH.MODE, "model": BATCH.MODEL_ID,
        "revision": BATCH.REVISION, "profile": BATCH.PROFILE, "base_url": BATCH.BASE_URL,
        "max_tokens": BATCH.MAX_TOKENS, "temperature": 0, "batch_size": BATCH.BATCH_SIZE,
        "arms": list(BATCH.ARMS), "automatic_retries": 0,
        "batch_http_timeout_seconds": BATCH.BATCH_HTTP_TIMEOUT_SECONDS,
        "soft_wall_seconds": BATCH.SOFT_WALL_SECONDS,
        "service_decode_max_time_seconds": 240, "per_request_soft_expectation_seconds": 300,
        "soft_expectation_is_not_an_enforced_generation_timeout": True,
        "prompts": BATCH.PROMPTS, "formal_quality_scoring_eligible": False,
        "scoring": "not_performed", "human_editing_time": None,
    }
    require(all(plan.get(key) == value for key, value in expected.items()), "frozen_profile_mismatch")
    require(plan.get("gold_reference", {}).get("status") == "draft", "draft_gold_status_mismatch")


def _verify_dataset(run_dir, dataset, plan):
    require(set(plan.get("dataset_files", {})) == set(DATASET_FILES), "unexpected_dataset_file_manifest")
    for name in DATASET_FILES:
        reference = plan["dataset_files"][name]
        require(isinstance(reference, dict), "invalid_dataset_file_reference")
        # reference.path is provenance only; never resolve or open the old path.
        check_sha(dataset, name, reference.get("sha256"), "relocated_dataset_sha256_mismatch:" + name)
    gold_path = check_sha(dataset, "private/gold-review.draft.json",
                          plan["gold_reference"].get("sha256"), "gold_reference_sha256_mismatch")
    require(read(gold_path).get("status") == "draft", "relocated_gold_is_not_draft")
    check_sha(dataset, "editing-subset.json", plan.get("editing_subset_sha256"),
              "pre_registered_editing_subset_sha256_mismatch")
    require(set(plan.get("frozen_files", {})) == set(FROZEN_FILES), "unexpected_frozen_file_manifest")
    for name in FROZEN_FILES:
        check_sha(run_dir, name, plan["frozen_files"].get(name), "frozen_file_sha256_mismatch:" + name)
    require(read(run_dir / "editing-subset.json") == read(dataset / "editing-subset.json"),
            "frozen_and_relocated_editing_subset_differ")
    require(read(run_dir / "dataset-manifest.json") == read(dataset / "manifest.json"),
            "frozen_and_relocated_dataset_manifest_differ")
    require(read(run_dir / "source-manifest.json") == read(dataset / "source-manifest.json"),
            "frozen_and_relocated_source_manifest_differ")
    rows = BATCH.validate_rows(read(run_dir / "inputs.json"))
    all_rows = [json.loads(line) for line in (dataset / "inputs.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    BATCH.validate_rows(all_rows)
    by_case = {row["case_id"]: row for row in all_rows}
    require(all(by_case.get(row["case_id"]) == row for row in rows), "frozen_inputs_differ_from_dataset")
    require(
        BATCH.digest(rows) == plan.get("public_inputs_sha256")
        and [row["case_id"] for row in rows] == plan.get("case_ids")
        and len(rows) == plan.get("planned_tasks")
        and len({row["event_group_id"] for row in rows}) == plan.get("planned_event_groups")
        and all(row["split"] == plan.get("split") for row in rows)
        and len(rows) * len(BATCH.ARMS) == plan.get("planned_requests")
        and len(plan.get("requests", [])) == plan.get("planned_requests"),
        "frozen_task_denominator_mismatch",
    )
    return rows


def _verify_requests(run_dir, rows, plan):
    identifiers = set()
    any_started = False
    for index, request in enumerate(plan["requests"]):
        row, arm = rows[index // 2], BATCH.ARMS[index % 2]
        identifier = request.get("request_id")
        require(isinstance(identifier, str) and BATCH.ID_PATTERN.fullmatch(identifier)
                and identifier not in identifiers, "unsafe_or_duplicate_request_id")
        identifiers.add(identifier)
        identity = BATCH.digest({
            "run_id": plan["run_id"], "case_id": row["case_id"], "arm": arm,
            "input": row["input"], "prompt": plan["prompts"][arm],
        })
        require(
            identifier == "wab-" + identity[:48]
            and request.get("case_id") == row["case_id"]
            and request.get("event_group_id") == row["event_group_id"]
            and request.get("arm") == arm
            and request.get("batch_index") == index // BATCH.BATCH_SIZE
            and request.get("public_input_sha256") == BATCH.digest(row["input"])
            and request.get("request_sha256") == BATCH.digest(BATCH.payload_for(plan, row, arm, identifier)),
            "frozen_request_payload_or_identity_mismatch",
        )
        folder = BATCH.case_dir(run_dir, request)
        start_path, meta_path = folder / "start.json", folder / "metadata.json"
        if start_path.exists():
            any_started = True
            start = read(local_file(run_dir, str(start_path.relative_to(run_dir))))
            require(all(start.get(key) == request[key] for key in IDENTITY_FIELDS),
                    "started_request_identity_mismatch")
            require(isinstance(start.get("started_at"), str) and bool(start["started_at"]),
                    "started_request_timestamp_missing")
            require(start.get("client_submission_attempts", start.get("request_attempts")) == 1,
                    "unexpected_client_submission_attempts")
        elif any((folder / name).exists() for name in ("metadata.json", "result.json", "response.json",
                                                     "draft.md", "draft.partial.md")):
            raise ValueError("request_artifact_without_durable_start")
        if not meta_path.exists():
            # Raw uncommitted results can be in flight. They remain unknown, never completed.
            if (folder / "result.json").exists():
                raw = read(local_file(run_dir, str((folder / "result.json").relative_to(run_dir))))
                require(raw.get("request_id") == identifier and raw.get("status") in ("completed", "failed"),
                        "uncommitted_result_identity_mismatch")
            continue
        meta = read(local_file(run_dir, str(meta_path.relative_to(run_dir))))
        require(meta.get("status") in ("completed", "failed"), "invalid_terminal_metadata_status")
        require(all(meta.get(key) == request[key] for key in IDENTITY_FIELDS), "terminal_request_identity_mismatch")
        require(meta.get("started_at") == start["started_at"], "terminal_start_timestamp_mismatch")
        require(meta.get("client_submission_attempts") == 1, "terminal_client_submission_attempts_mismatch")
        relative = str(folder.relative_to(run_dir))
        result_file = check_sha(run_dir, relative + "/result.json", meta.get("result_sha256"),
                                "cached_result_sha256_mismatch")
        result = read(result_file)
        require(result.get("request_id") == identifier and result.get("status") in ("completed", "failed"),
                "terminal_result_identity_mismatch")
        require(meta.get("gpu_generation_attempts") == result.get("attempt_count"),
                "gpu_attempt_count_mismatch")
        response = result.get("response")
        if isinstance(response, dict):
            response_file = check_sha(run_dir, relative + "/response.json", meta.get("response_sha256"),
                                      "response_sha256_mismatch")
            require(read(response_file) == response, "response_differs_from_cached_result")
        else:
            require(not meta.get("response_sha256") and not (folder / "response.json").exists(),
                    "unexpected_response_artifact")
        if meta["status"] == "completed":
            require(result["status"] == "completed", "completed_metadata_for_failed_result")
            text, usage = BATCH.checked_response(response)
            draft = check_sha(run_dir, relative + "/draft.md", meta.get("draft_sha256"), "draft_sha256_mismatch")
            require(draft.read_text(encoding="utf-8") == text and meta.get("usage") == usage
                    and meta.get("finish_reason") == "stop", "completed_draft_or_usage_differs_from_response")
            require(not meta.get("partial_draft_sha256") and not (folder / "draft.partial.md").exists(),
                    "partial_draft_for_completed_result")
        else:
            require(isinstance(meta.get("error_type"), str) and bool(meta["error_type"]),
                    "failed_metadata_reason_missing")
            require(not meta.get("draft_sha256") and not (folder / "draft.md").exists(),
                    "completed_draft_for_failed_result")
            if meta.get("partial_draft_sha256"):
                partial = check_sha(run_dir, relative + "/draft.partial.md", meta["partial_draft_sha256"],
                                    "partial_draft_sha256_mismatch")
                choices = response.get("choices") if isinstance(response, dict) else None
                require(isinstance(choices, list) and bool(choices) and isinstance(choices[0], dict),
                        "partial_draft_without_response_choice")
                message = choices[0].get("message")
                require(isinstance(message, dict) and partial.read_text(encoding="utf-8") == message.get("content"),
                        "partial_draft_differs_from_response")
            else:
                require(not (folder / "draft.partial.md").exists(), "unrecorded_partial_draft")
    return any_started


def _verify_health(run_dir, plan, any_started):
    health_path = run_dir / "health.json"
    require(health_path.exists() or not any_started, "started_requests_without_frozen_health")
    if health_path.exists():
        seal = read(local_file(run_dir, "health-seal.json"))
        check_sha(run_dir, "health.json", seal.get("sha256"), "frozen_health_sha256_mismatch")
        health = read(health_path)
        require(health.get("status") == "ok", "first_health_was_not_ready")
        require(all(health.get(key) == value for key, value in plan["service_profile"].items()),
                "first_health_differs_from_complete_frozen_service_profile")


def verify_portable(run_dir, dataset):
    """Verify only local bytes, mapping dataset files to an explicit new root."""
    run_dir, dataset = Path(run_dir).resolve(), Path(dataset).resolve()
    require(run_dir.is_dir() and dataset.is_dir(), "explicit_run_and_dataset_directories_required")
    seal = read(local_file(run_dir, "plan-seal.json"))
    check_sha(run_dir, "plan.json", seal.get("sha256"), "frozen_plan_sha256_mismatch")
    plan = read(run_dir / "plan.json")
    _verify_profile(plan)
    rows = _verify_dataset(run_dir, dataset, plan)
    any_started = _verify_requests(run_dir, rows, plan)
    _verify_health(run_dir, plan, any_started)
    return plan, rows


def status_portable(run_dir, dataset):
    """Same accounting fields as the frozen runner, with no old-path reads."""
    run_dir = Path(run_dir).resolve()
    plan, _ = verify_portable(run_dir, dataset)
    states, pairs = [], {}
    for request in plan["requests"]:
        folder = BATCH.case_dir(run_dir, request)
        if (folder / "metadata.json").exists():
            state = read(folder / "metadata.json")["status"]
        else:
            state = "unknown" if (folder / "start.json").exists() else "not_run"
        states.append(dict(request, status=state))
        pairs.setdefault(request["case_id"], []).append(state)
    counts = {value: sum(r["status"] == value for r in states)
              for value in ("completed", "failed", "unknown", "not_run")}
    return {
        "mode": BATCH.MODE, "run_dir": str(run_dir),
        "planned_tasks": plan["planned_tasks"], "planned_event_groups": plan["planned_event_groups"],
        "planned_requests": plan["planned_requests"],
        "started_requests": plan["planned_requests"] - counts["not_run"],
        "completed_requests": counts["completed"], "failed_requests": counts["failed"],
        "unknown_requests": counts["unknown"], "not_run": counts["not_run"],
        "fully_paired_tasks": sum(values == ["completed", "completed"] for values in pairs.values()),
        "scoring": "not_performed", "human_editing_time": None,
        "formal_quality_scoring_eligible": False, "requests": states,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path,
                        help="Explicit local dataset root; original absolute provenance paths are never opened.")
    args = parser.parse_args(argv)
    try:
        result = status_portable(args.run_dir, args.dataset)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "invalid", "error_type": type(exc).__name__, "reason": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    compact = {key: value for key, value in result.items() if key != "requests"}
    print(json.dumps(dict(compact, status="verified_local_bytes"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
