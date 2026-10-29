"""Synthetic offline runner checks; these fixtures are not model quality evidence."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "evaluation/run_writing_batch.py"
SPEC = importlib.util.spec_from_file_location("writing_batch_offline_checks", SOURCE)
BATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BATCH)


def fixture(folder, count=2):
    dataset = Path(folder) / "synthetic-dataset"
    (dataset / "private").mkdir(parents=True)
    rows = []
    for i in range(count):
        rows.append({
            "case_id": "synthetic-case-" + str(i),
            "event_group_id": "synthetic-event-" + str(i // 2),
            "split": "evaluation",
            "input": {
                "topic": "synthetic topic " + str(i),
                "requirements": {
                    "language": "zh-CN", "audience": "synthetic reader", "target_word_count": 300,
                    "max_sections": 2, "as_of": "2026-10-01T23:59:59+08:00",
                    "task_description": "Only a synthetic engineering fixture.",
                    "source_documents": [
                        {"source_id": "source-" + str(j), "news_id": "news-" + str(i // 2) + "-" + str(j),
                         "url": "https://example.invalid/" + str(i // 2) + "/" + str(j),
                         "title": "Synthetic title", "published_at": "2026-10-01",
                         "content": "Synthetic public news for group " + str(i // 2) + " source " + str(j)}
                        for j in (1, 2)
                    ],
                },
            },
        })
    (dataset / "inputs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (dataset / "manifest.json").write_text(json.dumps({"synthetic": True}))
    (dataset / "source-manifest.json").write_text(json.dumps({"synthetic": True}))
    (dataset / "editing-subset.json").write_text(json.dumps({
        "schema_version": 1, "selection": "pre_generation_synthetic_fixture",
        "case_ids": [r["case_id"] for r in rows], "human_timings": None,
    }))
    (dataset / "private/gold-review.draft.json").write_text(json.dumps({
        "status": "draft", "cases": [{"private": "PRIVATE_GOLD_NEVER_SENT"}],
    }))
    return dataset, rows


def response(text="Synthetic raw draft.", finish="stop"):
    return {
        "model": BATCH.MODEL_ID,
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }


class FakeTransport:
    def __init__(self):
        self.posts, self.gets, self.cache = [], [], {}
        self.health_value = dict(BATCH.expected_service_profile(), status="ok")
        self.post_error = False
        self.pending = False
        self.missing_code = 404
        self.fail_first = False
        self.on_post = None
        self.start_run = None

    def health(self):
        return 200, self.health_value

    def post(self, requests):
        self.posts.append(copy.deepcopy(requests))
        if self.start_run:
            plan, _ = BATCH.verify(self.start_run)
            for payload in requests:
                request = next(r for r in plan["requests"] if r["request_id"] == payload["request_id"])
                assert (BATCH.case_dir(self.start_run, request) / "start.json").is_file()
        results = []
        for index, request in enumerate(requests):
            result = {"request_id": request["request_id"], "status": "completed",
                      "response": response(), "cache_hit": False, "attempt_count": 1,
                      "attempts": [{"phase": "batch", "batch_size": len(requests)}]}
            if self.pending:
                result = {"request_id": request["request_id"], "status": "pending"}
            if self.fail_first and not index:
                result.update(status="failed", response=response("Truncated preserved bytes", "length"))
            self.cache[request["request_id"]] = result
            results.append(result)
        if self.on_post:
            self.on_post()
        if self.post_error:
            raise TimeoutError("Synthetic timeout after durable server completion.")
        return 200, {"profile": BATCH.PROFILE, "results": results}

    def get(self, request_id):
        self.gets.append(request_id)
        if request_id not in self.cache:
            return self.missing_code, {}
        value = self.cache[request_id]
        return (409 if value["status"] == "pending" else 200), copy.deepcopy(value)


class WritingBatchRunnerTests(unittest.TestCase):
    def prepared(self, folder, count=2):
        dataset, rows = fixture(folder, count)
        out = Path(folder) / "synthetic-run"
        BATCH.prepare(dataset, out, limit=count)
        return out, rows, dataset

    def execute(self, out, transport, clock=lambda: 1000):
        with patch("sys.stdout", new=io.StringIO()):
            return BATCH.run(out, transport=transport, clock=clock)

    def test_full_denominator_offline_freeze_and_private_boundary(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(BATCH.urllib.request, "build_opener") as opener:
            out, rows, dataset = self.prepared(folder, 100)
            opener.assert_not_called()
            plan, frozen = BATCH.verify(out)
            self.assertEqual(frozen, rows)
            self.assertEqual((plan["planned_tasks"], plan["planned_event_groups"], plan["planned_requests"]), (100, 50, 200))
            self.assertEqual(len({r["request_id"] for r in plan["requests"]}), 200)
            self.assertTrue(all(BATCH.ID_PATTERN.fullmatch(r["request_id"]) for r in plan["requests"]))
            self.assertEqual(plan["gold_reference"]["status"], "draft")
            self.assertFalse(plan["formal_quality_scoring_eligible"])
            self.assertEqual(plan["editing_subset_sha256"], BATCH.sha(dataset / "editing-subset.json"))
            self.assertEqual(plan["automatic_retries"], 0)
            self.assertEqual(plan["service_decode_max_time_seconds"], 240)
            self.assertEqual(plan["per_request_soft_expectation_seconds"], 300)
            self.assertEqual(plan["batch_http_timeout_seconds"], 900)
            self.assertEqual(plan["soft_wall_seconds"], 7200)
            payloads = [BATCH.payload_for(plan, rows[i // 2], r["arm"], r["request_id"])
                        for i, r in enumerate(plan["requests"])]
            self.assertNotIn("PRIVATE_GOLD_NEVER_SENT", json.dumps(payloads))
            self.assertNotIn("PRIVATE_GOLD_NEVER_SENT", (out / "plan.json").read_text())
            for i in range(0, 200, 2):
                self.assertEqual(payloads[i]["messages"][1], payloads[i + 1]["messages"][1])
            self.assertEqual(BATCH.status(out)["not_run"], 200)

    def test_batch_four_pre_registered_completed_skip_on_continue(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder)
            transport = FakeTransport()
            transport.start_run = out
            result = self.execute(out, transport)
            self.assertEqual(len(transport.posts), 1)
            self.assertEqual(len(transport.posts[0]), 4)
            self.assertEqual([r["messages"][0]["content"] for r in transport.posts[0]],
                             [BATCH.PROMPTS[a] for a in ("baseline", "candidate", "baseline", "candidate")])
            self.assertEqual(result["completed_requests"], 4)
            self.assertEqual(result["fully_paired_tasks"], 2)
            self.assertIsNone(result["human_editing_time"])
            self.assertEqual(result["scoring"], "not_performed")
            again = self.execute(out, transport)
            self.assertEqual(again["completed_requests"], 4)
            self.assertEqual(len(transport.posts), 1)
            self.assertEqual(transport.gets, [])

    def test_timeout_recovers_cache_without_second_post(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder)
            transport = FakeTransport()
            transport.post_error = True
            result = self.execute(out, transport)
            self.assertEqual(result["completed_requests"], 4)
            self.assertEqual(len(transport.posts), 1)
            self.assertEqual(len(transport.gets), 4)
            plan, _ = BATCH.verify(out)
            self.assertTrue(all(BATCH.read(BATCH.case_dir(out, r) / "metadata.json")["cache_recovered"]
                                for r in plan["requests"]))

    def test_pending_retained_then_continue_reads_cache_only(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder, 4)
            transport = FakeTransport()
            transport.pending = True
            result = self.execute(out, transport)
            self.assertEqual((result["unknown_requests"], result["not_run"]), (4, 4))
            self.assertEqual(len(transport.posts), 1)
            again = self.execute(out, transport)
            self.assertEqual(again["unknown_requests"], 4)
            self.assertEqual(len(transport.posts), 1)
            # Complete the original first batch without any model resubmission.
            for request_id in list(transport.cache):
                transport.cache[request_id] = {
                    "request_id": request_id, "status": "completed", "response": response(), "cache_hit": True,
                }
            transport.pending = False
            final = self.execute(out, transport)
            self.assertEqual(final["completed_requests"], 8)
            self.assertEqual(len(transport.posts), 2)
            first_ids = {r["request_id"] for r in transport.posts[0]}
            self.assertTrue(first_ids.isdisjoint({r["request_id"] for r in transport.posts[1]}))

    def test_started_404_is_unknown_not_resent(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder)
            plan, _ = BATCH.verify(out)
            request = plan["requests"][0]
            BATCH.write_new(BATCH.case_dir(out, request) / "start.json",
                            dict(request, started_at=BATCH.now(), request_attempts=1))
            transport = FakeTransport()
            result = self.execute(out, transport)
            self.assertEqual((result["unknown_requests"], result["not_run"]), (1, 3))
            self.assertEqual(transport.posts, [])
            self.execute(out, transport)
            self.assertEqual(transport.posts, [])
            self.assertEqual(transport.gets, [request["request_id"], request["request_id"]])

    def test_length_preserved_and_failure_stays_in_denominator(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder)
            transport = FakeTransport()
            transport.fail_first = True
            result = self.execute(out, transport)
            self.assertEqual((result["planned_requests"], result["completed_requests"], result["failed_requests"]), (4, 3, 1))
            plan, _ = BATCH.verify(out)
            case = BATCH.case_dir(out, plan["requests"][0])
            self.assertEqual((case / "draft.partial.md").read_text(), "Truncated preserved bytes")
            self.assertFalse((case / "draft.md").exists())
            self.assertEqual(BATCH.read(case / "response.json")["choices"][0]["finish_reason"], "length")
            self.assertEqual(result["fully_paired_tasks"], 1)
            self.execute(out, transport)
            self.assertEqual(len(transport.posts), 1)

    def test_durable_raw_result_completes_local_commit_without_post(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder)
            plan, _ = BATCH.verify(out)
            transport = FakeTransport()
            for request in plan["requests"]:
                case = BATCH.case_dir(out, request)
                BATCH.write_new(case / "start.json", dict(request, started_at=BATCH.now(), request_attempts=1))
                BATCH.write_new(case / "result.json", {
                    "request_id": request["request_id"], "status": "completed", "response": response(),
                })
            result = self.execute(out, transport)
            self.assertEqual(result["completed_requests"], 4)
            self.assertEqual(transport.posts, [])
            self.assertEqual(transport.gets, [])

    def test_completed_draft_tampering_blocks_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder)
            transport = FakeTransport()
            self.execute(out, transport)
            plan, _ = BATCH.verify(out)
            (BATCH.case_dir(out, plan["requests"][0]) / "draft.md").write_text("changed")
            with self.assertRaisesRegex(ValueError, "draft_hash_mismatch"):
                self.execute(out, transport)
            self.assertEqual(len(transport.posts), 1)

    def test_source_and_plan_input_changes_block_before_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder)
            transport = FakeTransport()
            (out / "inputs.json").write_text("[]")
            with self.assertRaisesRegex(ValueError, "frozen_dataset_hash_mismatch"):
                self.execute(out, transport)
            self.assertEqual(transport.posts, [])

    def test_cross_split_group_duplicate_id_and_private_requirement_rejected(self):
        for alteration in ("cross_split", "duplicate", "private"):
            with self.subTest(alteration=alteration), tempfile.TemporaryDirectory() as folder:
                dataset, rows = fixture(folder)
                if alteration == "cross_split":
                    rows[1]["split"] = "development"
                elif alteration == "duplicate":
                    rows[1]["case_id"] = rows[0]["case_id"]
                else:
                    rows[0]["input"]["requirements"]["private_gold"] = "must never enter request"
                (dataset / "inputs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
                with self.assertRaises(ValueError):
                    BATCH.prepare(dataset, Path(folder) / "out", limit=1)

    def test_gold_cannot_be_automatically_promoted_and_subset_is_frozen(self):
        with tempfile.TemporaryDirectory() as folder:
            dataset, _ = fixture(folder)
            gold = dataset / "private/gold-review.draft.json"
            gold.write_text(json.dumps({"status": "approved"}))
            with self.assertRaisesRegex(ValueError, "requires_draft_gold"):
                BATCH.prepare(dataset, Path(folder) / "run", limit=2)
        with tempfile.TemporaryDirectory() as folder:
            out, _, dataset = self.prepared(folder)
            (dataset / "editing-subset.json").write_text(json.dumps({"case_ids": []}))
            with self.assertRaisesRegex(ValueError, "pre_generation_editing_subset_changed"):
                BATCH.verify(out)

    def test_soft_wall_stops_new_batches_but_preserves_inflight_result(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder, 4)
            current = [1000]
            transport = FakeTransport()
            transport.on_post = lambda: current.__setitem__(0, 8201)
            result = self.execute(out, transport, clock=lambda: current[0])
            self.assertEqual(result["stop_reason"], "soft_wall_budget_reached")
            self.assertEqual((result["completed_requests"], result["not_run"]), (4, 4))
            self.assertEqual(len(transport.posts), 1)
            again = self.execute(out, transport, clock=lambda: current[0])
            self.assertEqual(again["not_run"], 4)
            self.assertEqual(len(transport.posts), 1)

    def test_bad_health_profile_blocks_all_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder)
            transport = FakeTransport()
            transport.health_value["revision"] = "other-model"
            result = self.execute(out, transport)
            self.assertEqual(result["stop_reason"], "blocked_ValueError")
            self.assertEqual(result["started_requests"], 0)
            self.assertEqual(result["not_run"], 4)
            self.assertEqual(transport.posts, [])

    def test_gpu_attempts_are_separate_from_single_client_submission(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder)
            transport = FakeTransport()
            original_post = transport.post
            def post_with_oom_split(requests):
                code, envelope = original_post(requests)
                for result in envelope["results"]:
                    result["attempt_count"] = 2
                    result["attempts"].append({"phase": "oom_split", "batch_size": 2})
                return code, envelope
            transport.post = post_with_oom_split
            self.execute(out, transport)
            plan, _ = BATCH.verify(out)
            meta = BATCH.read(BATCH.case_dir(out, plan["requests"][0]) / "metadata.json")
            result = BATCH.read(BATCH.case_dir(out, plan["requests"][0]) / "result.json")
            self.assertEqual(meta["client_submission_attempts"], 1)
            self.assertEqual(meta["gpu_generation_attempts"], 2)
            self.assertEqual(result["attempt_count"], 2)
            self.assertEqual(len(result["attempts"]), 2)

    def test_service_source_or_generation_policy_change_blocks_new_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder, 4)
            transport = FakeTransport()
            current = [1000]
            transport.on_post = lambda: current.__setitem__(0, 8201)
            first = self.execute(out, transport, clock=lambda: current[0])
            self.assertEqual(first["not_run"], 4)
            self.assertTrue((out / "health.json").is_file())
            transport.health_value["source_sha256"] = "c" * 64
            result = self.execute(out, transport, clock=lambda: 1001)
            self.assertEqual(result["stop_reason"], "blocked_ValueError")
            self.assertEqual(len(transport.posts), 1)

    def test_concurrent_runner_lock_rejects_second_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _, _ = self.prepared(folder)
            with BATCH.run_lock(out):
                with self.assertRaisesRegex(RuntimeError, "another_runner_is_active"):
                    self.execute(out, FakeTransport())


if __name__ == "__main__":
    unittest.main()
