"""Synthetic offline fixtures for the direct-text pilot; no real model quality data."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "evaluation/run_writing_text_ab.py"
SPEC = importlib.util.spec_from_file_location("writing_text_ab_synthetic_checks", SOURCE)
PILOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PILOT)


def fixture(folder):
    root = Path(folder)
    dataset = root / "synthetic-dataset"
    dataset.mkdir()
    rows = [{"case_id": f"synthetic-case-{i}", "event_group_id": f"synthetic-event-{i}",
             "split": "development", "input": {"topic": f"synthetic topic {i}",
             "requirements": {"source_documents": [{"content": f"synthetic public source {i}"}]}}}
            for i in (1, 2)]
    (dataset / "inputs.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    gold = {"status": "approved", "reviewer": "synthetic-auditor", "reviewed_at": "2026-01-01T00:00:00Z",
            "inputs_sha256": hashlib.sha256(PILOT.canonical(rows)).hexdigest(),
            "cases": [{"case_id": row["case_id"], "source_facts": ["PRIVATE_GOLD_MUST_NOT_LEAK"]} for row in rows]}
    gold_file = root / "synthetic-gold.json"
    gold_file.write_text(json.dumps(gold))
    return dataset, gold_file, rows


def health():
    return {"status": "ok", "model": PILOT.MODEL_ID, "revision": PILOT.REVISION,
            "quantization": "nf4-double-quant"}


def response(**updates):
    value = {"model": PILOT.MODEL_ID, "choices": [
        {"message": {"role": "assistant", "content": "## Synthetic draft\nActual fixture bytes."},
         "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}}
    value.update(updates)
    return value


class TextABTests(unittest.TestCase):
    def prepared(self, folder):
        dataset, gold, rows = fixture(folder)
        out = Path(folder) / "synthetic-run"
        PILOT.prepare(dataset, gold, out)
        return out, rows

    def test_prepare_is_offline_and_binds_only_gold_identity(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(PILOT.urllib.request, "urlopen") as call:
            out, rows = self.prepared(folder)
            call.assert_not_called()
            plan, frozen = PILOT.verify(out)
            self.assertEqual(frozen, rows)
            self.assertEqual(plan["scope"], "direct writing Prompt development pilot, not native RWR pipeline")
            self.assertEqual(plan["planned_requests"], 4)
            self.assertEqual(plan["max_tokens"], 2048)
            self.assertEqual(plan["timeout_seconds"], 300)
            self.assertEqual(plan["automatic_retries"], 0)
            self.assertNotIn("PRIVATE_GOLD_MUST_NOT_LEAK", (out / "plan.json").read_text())
            self.assertFalse((out / "gold.json").exists())

    def test_unapproved_gold_and_input_hash_mismatch_rejected(self):
        for change in ({"status": "pending_human_review"}, {"inputs_sha256": "0" * 64}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                dataset, gold_file, _ = fixture(folder)
                gold = json.loads(gold_file.read_text())
                gold.update(change)
                gold_file.write_text(json.dumps(gold))
                with self.assertRaises(ValueError):
                    PILOT.prepare(dataset, gold_file, Path(folder) / "not-created")
                self.assertFalse((Path(folder) / "not-created").exists())

    def test_prepare_never_overwrites_and_requires_exactly_two_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            dataset, gold_file, rows = fixture(folder)
            out = Path(folder) / "existing"
            out.mkdir()
            with self.assertRaises(ValueError):
                PILOT.prepare(dataset, gold_file, out)
            (dataset / "inputs.jsonl").write_text(json.dumps(rows[0]) + "\n")
            with self.assertRaises(ValueError):
                PILOT.prepare(dataset, gold_file, Path(folder) / "other")

    def test_changed_public_inputs_and_plan_are_rejected(self):
        for filename in ("inputs.json", "plan.json"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as folder:
                out, _ = self.prepared(folder)
                with (out / filename).open("a") as stream:
                    stream.write(" ")
                with self.assertRaises(ValueError):
                    PILOT.verify(out)

    def test_four_requests_receive_identical_public_inputs_and_preserve_text(self):
        with tempfile.TemporaryDirectory() as folder:
            out, rows = self.prepared(folder)
            sent = []

            def fake_open(req, timeout):
                if isinstance(req, str):
                    self.assertEqual(req, PILOT.HEALTH_URL)
                    return io.BytesIO(json.dumps(health()).encode())
                self.assertEqual(timeout, 300)
                self.assertEqual(req.full_url, PILOT.URL)
                body = json.loads(req.data)
                sent.append(body)
                self.assertEqual(body["max_tokens"], 2048)
                self.assertEqual(body["model"], PILOT.MODEL_ID)
                self.assertNotIn("PRIVATE_GOLD_MUST_NOT_LEAK", req.data.decode())
                return io.BytesIO(json.dumps(response()).encode())

            with patch.object(PILOT.urllib.request, "urlopen", side_effect=fake_open):
                result = PILOT.run(out)
            self.assertEqual(result["completed_requests"], 4)
            self.assertEqual(result["attempted_requests"], 4)
            self.assertEqual(result["scoring"], "not_performed")
            self.assertIsNone(result["human_editing_time"])
            for index, row in enumerate(rows):
                a, b = sent[index * 2:index * 2 + 2]
                self.assertEqual(a["messages"][1], b["messages"][1])
                self.assertEqual(json.loads(a["messages"][1]["content"]), row["input"])
                self.assertNotEqual(a["messages"][0], b["messages"][0])
                for arm in PILOT.ARMS:
                    draft = out / "cases" / row["case_id"] / arm / "draft.md"
                    self.assertEqual(draft.read_text(), response()["choices"][0]["message"]["content"])

    def test_wrong_identity_length_and_empty_response_are_not_completed(self):
        invalid = [
            response(model="wrong-model"),
            response(choices=[{"message": {"role": "assistant", "content": "synthetic"}, "finish_reason": "length"}]),
            response(choices=[{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]),
        ]
        for value in invalid:
            with self.subTest(response=value), self.assertRaises(ValueError):
                PILOT.checked_response(value)

    def test_failed_generation_preserves_denominator_and_raw_response(self):
        with tempfile.TemporaryDirectory() as folder:
            out, rows = self.prepared(folder)
            values = [health()] + [response(model="wrong-model")] * 4
            with patch.object(PILOT.urllib.request, "urlopen",
                              side_effect=[io.BytesIO(json.dumps(v).encode()) for v in values]):
                result = PILOT.run(out)
            self.assertEqual(result["attempted_requests"], 4)
            self.assertEqual(result["failed_requests"], 4)
            self.assertEqual(result["completed_requests"], 0)
            for row in rows:
                for arm in PILOT.ARMS:
                    case_dir = out / "cases" / row["case_id"] / arm
                    self.assertTrue((case_dir / "response.json").exists())
                    self.assertFalse((case_dir / "draft.md").exists())

    def test_unavailable_health_blocks_without_generation_and_prevents_rerun(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _ = self.prepared(folder)
            with patch.object(PILOT.urllib.request, "urlopen", side_effect=OSError("synthetic offline")) as call:
                result = PILOT.run(out)
                self.assertEqual(call.call_count, 1)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["attempted_requests"], 0)
            self.assertEqual(result["not_run"], 4)
            with patch.object(PILOT.urllib.request, "urlopen") as call, self.assertRaises(FileExistsError):
                PILOT.run(out)
            call.assert_not_called()

    def test_one_timeout_is_not_retried_and_other_arms_continue(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _ = self.prepared(folder)
            values = [io.BytesIO(json.dumps(health()).encode()), TimeoutError("synthetic timeout")]
            values.extend(io.BytesIO(json.dumps(response()).encode()) for _ in range(3))
            with patch.object(PILOT.urllib.request, "urlopen", side_effect=values) as call:
                result = PILOT.run(out)
            self.assertEqual(call.call_count, 5)
            self.assertEqual(result["attempted_requests"], 4)
            self.assertEqual(result["failed_requests"], 1)
            self.assertEqual(result["completed_requests"], 3)


if __name__ == "__main__":
    unittest.main()
