"""Synthetic portable-integrity checks, never real model quality evidence."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "evaluation/verify_writing_run_portable.py"
SPEC = importlib.util.spec_from_file_location("portable_writing_integrity_tests", SOURCE)
PORTABLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PORTABLE)
BATCH = PORTABLE.BATCH


def fixture(folder):
    dataset = Path(folder) / "original-dataset"
    (dataset / "private").mkdir(parents=True)
    rows = []
    for index in range(2):
        rows.append({
            "case_id": "synthetic-case-" + str(index),
            "event_group_id": "synthetic-group",
            "split": "evaluation",
            "input": {"topic": "Synthetic public writing task " + str(index), "requirements": {
                "language": "zh-CN", "audience": "synthetic", "target_word_count": 300,
                "max_sections": 2, "as_of": "2026-10-01T23:59:59+08:00",
                "task_description": "This fixture verifies bytes, not quality.",
                "source_documents": [
                    {"source_id": "source-" + str(i), "news_id": "news-" + str(i),
                     "url": "https://example.invalid/source-" + str(i),
                     "title": "Synthetic source", "published_at": "2026-10-01",
                     "content": "Synthetic public material " + str(i)}
                    for i in (1, 2)
                ],
            }},
        })
    (dataset / "inputs.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    (dataset / "manifest.json").write_text(json.dumps({"synthetic": True}))
    (dataset / "source-manifest.json").write_text(json.dumps({"synthetic": True}))
    (dataset / "editing-subset.json").write_text(json.dumps({
        "selection": "pre_generation_synthetic", "case_ids": ["synthetic-case-0"], "human_timings": None,
    }))
    (dataset / "private/gold-review.draft.json").write_text(json.dumps({
        "status": "draft", "private_fact": "PRIVATE_SYNTHETIC_GOLD",
    }))
    return dataset


def make_run(folder, states=("completed",) * 4):
    dataset = fixture(folder)
    out = Path(folder) / "original-run"
    BATCH.prepare(dataset, out, limit=2)
    plan, _ = BATCH.verify(out)
    if any(state != "not_run" for state in states):
        BATCH.freeze_health(out, dict(BATCH.expected_service_profile(), status="ok"), plan["service_profile"])
    for request, state in zip(plan["requests"], states):
        if state == "not_run":
            continue
        directory = BATCH.case_dir(out, request)
        BATCH.write_new(directory / "start.json", dict(request, started_at=BATCH.now(), client_submission_attempts=1))
        if state == "unknown":
            continue
        response = {
            "model": BATCH.MODEL_ID,
            "choices": [{"message": {"role": "assistant", "content": "Synthetic immutable raw draft."},
                         "finish_reason": "length" if state == "failed" else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result = {
            "request_id": request["request_id"], "status": "failed" if state in ("failed", "error") else "completed",
            "response": None if state == "error" else response,
            "attempt_count": 1, "attempts": [{"phase": "synthetic", "batch_size": 4}],
        }
        if state == "error":
            result["error"] = {"type": "synthetic_failure"}
        BATCH.save_result(out, request, result)
    return out, dataset


def relocate(folder, out, dataset):
    root = Path(folder) / "different-computer-layout"
    root.mkdir()
    new_out, new_dataset = root / "run-copy", root / "dataset-copy"
    # Both source and destination remain inside this fresh test-owned directory.
    out.rename(new_out)
    dataset.rename(new_dataset)
    return new_out, new_dataset


def rewrite(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def snapshot(folder):
    root = Path(folder)
    return {str(p.relative_to(root)): BATCH.sha(p) for p in root.rglob("*") if p.is_file()}


class WritingPortableVerifierTests(unittest.TestCase):
    def test_actual_temp_relocation_succeeds_without_old_absolute_path(self):
        with tempfile.TemporaryDirectory() as folder:
            out, dataset = make_run(folder)
            original_plan = (out / "plan.json").read_bytes()
            frozen_status = BATCH.status(out)
            out, dataset = relocate(folder, out, dataset)
            self.assertFalse(Path(BATCH.read(out / "plan.json")["dataset_path"]).exists())
            with self.assertRaises(FileNotFoundError):
                BATCH.verify(out)
            plan, rows = PORTABLE.verify_portable(out, dataset)
            self.assertEqual((out / "plan.json").read_bytes(), original_plan)
            self.assertEqual(plan["planned_requests"], 4)
            self.assertEqual(len(rows), 2)
            actual = PORTABLE.status_portable(out, dataset)
            expected = dict(frozen_status, run_dir=str(out.resolve()))
            self.assertEqual(actual, expected)

    def test_read_only_no_network_and_no_state_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            out, dataset = make_run(folder)
            out, dataset = relocate(folder, out, dataset)
            before = snapshot(folder)
            with patch.object(BATCH.urllib.request, "build_opener") as opener, \
                    patch.object(BATCH.urllib.request, "urlopen") as urlopen:
                PORTABLE.verify_portable(out, dataset)
                result = PORTABLE.status_portable(out, dataset)
                opener.assert_not_called()
                urlopen.assert_not_called()
            self.assertEqual(before, snapshot(folder))
            self.assertEqual(result["completed_requests"], 4)
            self.assertIsNone(result["human_editing_time"])
            self.assertFalse(result["formal_quality_scoring_eligible"])
            self.assertEqual(result["scoring"], "not_performed")
            self.assertFalse((out / ".runner.lock").exists())

    def test_prepared_run_needs_no_health_and_preserves_not_run(self):
        with tempfile.TemporaryDirectory() as folder:
            out, dataset = make_run(folder, ("not_run",) * 4)
            out, dataset = relocate(folder, out, dataset)
            result = PORTABLE.status_portable(out, dataset)
            self.assertEqual((result["planned_requests"], result["started_requests"], result["not_run"]), (4, 0, 4))
            self.assertEqual(result["completed_requests"], 0)

    def test_fixed_denominator_keeps_failure_unknown_and_not_run(self):
        with tempfile.TemporaryDirectory() as folder:
            out, dataset = make_run(folder, ("completed", "failed", "unknown", "not_run"))
            out, dataset = relocate(folder, out, dataset)
            result = PORTABLE.status_portable(out, dataset)
            self.assertEqual((result["completed_requests"], result["failed_requests"],
                              result["unknown_requests"], result["not_run"]), (1, 1, 1, 1))
            self.assertEqual(result["planned_requests"], 4)
            self.assertEqual(result["fully_paired_tasks"], 0)
            plan, _ = PORTABLE.verify_portable(out, dataset)
            partial = BATCH.case_dir(out, plan["requests"][1]) / "draft.partial.md"
            self.assertEqual(partial.read_text(), "Synthetic immutable raw draft.")

    def test_failure_without_response_is_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            out, dataset = make_run(folder, ("error", "completed", "completed", "completed"))
            out, dataset = relocate(folder, out, dataset)
            result = PORTABLE.status_portable(out, dataset)
            self.assertEqual((result["completed_requests"], result["failed_requests"]), (3, 1))

    def test_file_tampering_is_rejected_after_relocation(self):
        targets = (
            ("run", "plan.json"),
            ("run", "inputs.json"),
            ("dataset", "inputs.jsonl"),
            ("dataset", "private/gold-review.draft.json"),
            ("dataset", "editing-subset.json"),
            ("run", "cases/synthetic-case-0/baseline/result.json"),
            ("run", "cases/synthetic-case-0/baseline/response.json"),
            ("run", "cases/synthetic-case-0/baseline/draft.md"),
            ("run", "health.json"),
        )
        for location, name in targets:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                out, dataset = make_run(folder)
                out, dataset = relocate(folder, out, dataset)
                path = (out if location == "run" else dataset) / name
                with path.open("a", encoding="utf-8") as stream:
                    stream.write("\n")
                with self.assertRaises(ValueError):
                    PORTABLE.verify_portable(out, dataset)

    def test_start_metadata_and_gpu_attempt_identity_checks(self):
        for artifact, key, value in (
            ("start.json", "request_id", "different-id"),
            ("metadata.json", "request_sha256", "0" * 64),
            ("metadata.json", "gpu_generation_attempts", 2),
            ("metadata.json", "usage", {"completion_tokens": 999}),
        ):
            with self.subTest(artifact=artifact, key=key), tempfile.TemporaryDirectory() as folder:
                out, dataset = make_run(folder)
                out, dataset = relocate(folder, out, dataset)
                path = out / "cases/synthetic-case-0/baseline" / artifact
                payload = BATCH.read(path)
                payload[key] = value
                rewrite(path, payload)
                with self.assertRaises(ValueError):
                    PORTABLE.verify_portable(out, dataset)

    def test_resealed_health_must_match_complete_frozen_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            out, dataset = make_run(folder)
            out, dataset = relocate(folder, out, dataset)
            health = BATCH.read(out / "health.json")
            health["context_tokens"] = 8192
            rewrite(out / "health.json", health)
            rewrite(out / "health-seal.json", {"sha256": BATCH.sha(out / "health.json")})
            with self.assertRaisesRegex(ValueError, "complete_frozen_service_profile"):
                PORTABLE.verify_portable(out, dataset)

    def test_source_hash_or_service_profile_mismatch_rejected_without_editing_frozen_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            out, dataset = make_run(folder)
            out, dataset = relocate(folder, out, dataset)
            original_sha = BATCH.sha
            def different_runner(path):
                return "0" * 64 if Path(path).resolve() == PORTABLE.RUNNER_SOURCE else original_sha(path)
            with patch.object(BATCH, "sha", side_effect=different_runner):
                with self.assertRaisesRegex(ValueError, "runner_source_sha256_mismatch"):
                    PORTABLE.verify_portable(out, dataset)
            profile = copy.deepcopy(BATCH.expected_service_profile())
            profile["runtime_source_sha256"] = "0" * 64
            with patch.object(BATCH, "expected_service_profile", return_value=profile):
                with self.assertRaisesRegex(ValueError, "service_or_runtime_source"):
                    PORTABLE.verify_portable(out, dataset)

    def test_resealed_payload_change_still_fails_reconstruction(self):
        with tempfile.TemporaryDirectory() as folder:
            out, dataset = make_run(folder)
            out, dataset = relocate(folder, out, dataset)
            plan = BATCH.read(out / "plan.json")
            plan["requests"][0]["request_sha256"] = "0" * 64
            rewrite(out / "plan.json", plan)
            rewrite(out / "plan-seal.json", {"sha256": BATCH.sha(out / "plan.json")})
            with self.assertRaisesRegex(ValueError, "payload_or_identity"):
                PORTABLE.verify_portable(out, dataset)

    def test_explicit_dataset_is_required_and_never_falls_back_to_original(self):
        with tempfile.TemporaryDirectory() as folder:
            out, dataset = make_run(folder)
            empty = Path(folder) / "empty-dataset"
            empty.mkdir()
            with self.assertRaisesRegex(ValueError, "required_artifact_missing"):
                PORTABLE.verify_portable(out, empty)
            self.assertTrue(dataset.is_dir())

    def test_symlink_outside_explicit_dataset_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            out, dataset = make_run(folder)
            out, dataset = relocate(folder, out, dataset)
            target = dataset / "source-manifest.json"
            external = Path(folder) / "outside-root.json"
            external.write_bytes(target.read_bytes())
            target.unlink()
            target.symlink_to(external)
            with self.assertRaisesRegex(ValueError, "escapes_explicit_root"):
                PORTABLE.verify_portable(out, dataset)

    def test_cli_outputs_compact_counts_without_request_dump(self):
        with tempfile.TemporaryDirectory() as folder:
            out, dataset = make_run(folder)
            out, dataset = relocate(folder, out, dataset)
            with patch("sys.stdout", new=io.StringIO()) as stream:
                code = PORTABLE.main(["--run-dir", str(out), "--dataset", str(dataset)])
            value = json.loads(stream.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(value["status"], "verified_local_bytes")
            self.assertEqual(value["planned_requests"], 4)
            self.assertNotIn("requests", value)


if __name__ == "__main__":
    unittest.main()
