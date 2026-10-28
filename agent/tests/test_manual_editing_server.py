"""Synthetic fixtures only: no genuine human edits or measured resume values."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from evaluation.editing_time import score_records
from evaluation.manual_editing_server import EditingStore, make_server, write_new


class Clock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def package():
    return {"schema_version": 1, "experiment_id": "synthetic-timer-test-only",
        "gold_sha256": "a" * 64, "gold_status": "approved",
        "criterion_version": "synthetic-v1", "criterion_text": "Synthetic completion rule",
        "data_origin": "synthetic_test",
        "tasks": [{"blind_id": blind, "case_id": "synthetic-case", "event_group_id": "synthetic-event",
                   "arm": arm, "source_run_id": "synthetic-" + arm, "title": "Synthetic draft",
                   "draft_content": "Synthetic draft <script>not executable</script>",
                   "evidence": [{"title": "Synthetic source", "content": "Synthetic evidence", "url": "https://example.invalid"}]}
                  for blind, arm in (("W001", "baseline"), ("W002", "candidate"))]}


class ManualEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package_path = self.root / "synthetic-package.json"
        self.package_path.write_text(json.dumps(package()), encoding="utf-8")
        self.clock = Clock()
        self.store = EditingStore(self.package_path, self.root / "state", self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def action(self, action, blind="W001", **body):
        body["expected_revision"] = self.store.states[blind]["revision"]
        return self.store.action(blind, action, body)

    def test_blind_view_hides_arm_and_draft_before_start(self):
        self.assertNotIn("content", self.store.public("W001"))
        self.assertNotIn("arm", self.store.public("W001"))
        self.assertNotIn("source_run_id", self.store.public("W001"))
        self.assertNotIn("baseline", json.dumps(self.store.listing()))
        self.action("start", editor="synthetic-editor")
        self.assertIn("content", self.store.public("W001"))
        self.assertNotIn("arm", self.store.public("W001"))

    def test_real_event_pause_arithmetic_and_finished_artifact(self):
        self.action("start", editor="synthetic-editor")
        self.clock.advance(60)
        self.action("pause", content="synthetic saved draft")
        self.clock.advance(30)
        self.action("resume")
        self.clock.advance(60)
        view = self.action("finish", content="synthetic final")
        self.assertEqual(view["active_seconds"], 120)
        record = self.store.export()["records"][0]
        self.assertEqual(record["paused_seconds"], 30)
        self.assertEqual((self.root / "state/drafts/W001.md").read_text(), "synthetic final")
        self.assertIsNone(record["meets_criterion"])
        self.assertIsNone(record["reviewer"])
        self.assertEqual(self.store.export()["status"], "pending_human_review")

    def test_client_time_injection_revision_and_illegal_transition_rejected(self):
        with self.assertRaises(ValueError):
            self.store.action("W001", "start", {"expected_revision": 0, "editor": "synthetic",
                                               "started_at": "2020-01-01T00:00:00Z"})
        self.action("start", editor="synthetic")
        with self.assertRaises(ValueError):
            self.store.action("W001", "save", {"expected_revision": 0, "content": "synthetic"})
        self.action("pause", content="synthetic")
        with self.assertRaises(ValueError):
            self.action("finish", content="synthetic")
        with self.assertRaises(ValueError):
            self.action("save", content="synthetic")

    def test_restart_excludes_unobserved_gap_and_recovers_text(self):
        self.action("start", editor="synthetic")
        self.clock.advance(60)
        self.action("save", content="synthetic saved")
        self.clock.advance(60)
        self.store = EditingStore(self.package_path, self.root / "state", self.clock)
        self.assertEqual(self.store.public("W001")["status"], "paused")
        self.assertEqual(self.store.public("W001")["content"], "synthetic saved")
        self.assertEqual(self.store.public("W001")["active_seconds"], 60)
        self.clock.advance(60)
        self.action("resume")
        self.clock.advance(60)
        self.assertEqual(self.action("finish", content="synthetic final")["active_seconds"], 120)

    def test_readonly_export_load_does_not_recover_or_append_events(self):
        self.action("start", editor="synthetic")
        original = self.store.sequence
        self.clock.advance(60)
        snapshot = EditingStore(self.package_path, self.root / "state", self.clock, recover_running=False)
        self.assertEqual(snapshot.sequence, original)
        self.assertEqual(snapshot.public("W001")["status"], "running")
        self.assertIsNone(snapshot.export()["records"][0]["finished_at"])

    def test_finished_artifact_and_events_are_not_overwritten(self):
        self.action("start", editor="synthetic")
        self.action("finish", content="synthetic final")
        saved = (self.root / "state/drafts/W001.md").read_bytes()
        with self.assertRaises(ValueError):
            self.action("finish", content="changed")
        self.assertEqual((self.root / "state/drafts/W001.md").read_bytes(), saved)
        target = self.root / "new.json"
        write_new(target, {"synthetic": True})
        with self.assertRaises(FileExistsError):
            write_new(target, {"synthetic": False})

    def test_changed_package_cannot_reuse_session_directory(self):
        data = package()
        data["tasks"][0]["draft_content"] = "changed synthetic"
        self.package_path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            EditingStore(self.package_path, self.root / "state", self.clock)

    def test_export_requires_real_independent_quality_confirmation(self):
        for blind in ("W001", "W002"):
            self.action("start", blind, editor="synthetic")
            self.clock.advance(60)
            self.action("finish", blind, content="synthetic final")
        form = self.store.export()
        with self.assertRaises(ValueError):
            score_records(form)
        # Simulated signatures only in this unit-test fixture.
        approved = deepcopy(form)
        reviewed_at = (self.clock.value + timedelta(seconds=1)).isoformat()
        approved.update(status="approved", reviewer="synthetic-reviewer", reviewed_at=reviewed_at)
        for row in approved["records"]:
            row.update(reviewer="synthetic-reviewer", reviewed_at=reviewed_at, meets_criterion=True)
        self.assertEqual(score_records(approved)["paired_case_count"], 1)
        self.assertEqual(score_records(approved)["data_origin"], "synthetic_test")

    def test_orphan_transport_event_preserved_and_number_reused_safely(self):
        orphan = self.root / "state/events/00000001.tmp"
        orphan.write_text("partial synthetic crash artifact")
        self.action("start", editor="synthetic")
        archived = list(orphan.parent.glob("*.orphan-*"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(), "partial synthetic crash artifact")
        self.assertTrue((orphan.parent / "00000001.json").is_file())

    def test_start_rejects_same_editor_parallel_timer_but_allows_other_editor(self):
        self.action("start", "W001", editor="synthetic-editor")
        sequence = self.store.sequence
        with self.assertRaisesRegex(ValueError, "pause or finish"):
            self.action("start", "W002", editor=" synthetic-editor ")
        self.assertEqual(self.store.sequence, sequence)
        self.assertEqual(self.store.public("W002")["status"], "not_started")
        self.action("start", "W002", editor="synthetic-other-editor")
        self.assertEqual(self.store.public("W002")["status"], "running")

    def test_resume_requires_pausing_other_task_for_same_editor(self):
        self.action("start", "W001", editor="synthetic-editor")
        self.action("pause", "W001", content="synthetic first draft")
        self.action("start", "W002", editor="synthetic-editor")
        sequence = self.store.sequence
        with self.assertRaisesRegex(ValueError, "pause or finish"):
            self.action("resume", "W001")
        self.assertEqual(self.store.sequence, sequence)
        self.assertEqual(self.store.public("W001")["status"], "paused")
        self.action("pause", "W002", content="synthetic second draft")
        self.action("resume", "W001")
        self.assertEqual(self.store.public("W001")["status"], "running")
        self.assertEqual(self.store.public("W002")["status"], "paused")

    def test_http_rejects_foreign_origin_and_host(self):
        server = make_server(self.store, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            port = server.server_port
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            body = json.dumps({"expected_revision": 0, "editor": "synthetic"})
            conn.request("POST", "/api/tasks/W001/start", body,
                         {"Content-Type": "application/json", "Origin": "https://foreign.invalid"})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", "/api/tasks", headers={"Host": "foreign.invalid"})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("POST", "/api/tasks/W001/start", body,
                         {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read())
            self.assertNotIn("arm", payload)
            conn.close()
        finally:
            server.shutdown()
            worker.join(timeout=3)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
