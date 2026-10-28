"""Synthetic fixtures only; these numbers are not genuine human editing results."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.editing_time import main, make_template, score_records


def synthetic_records():
    gold = {"status": "pending_human_review", "cases": [
        {"case_id": "synthetic-case-1", "event_group_id": "synthetic-event-1"},
        {"case_id": "synthetic-case-2", "event_group_id": "synthetic-event-2"},
    ]}
    pack = make_template(gold, experiment_id="synthetic-unit-test-only",
                         criterion_version="synthetic-v1", criterion_text="Synthetic test criterion",
                         gold_sha256="a" * 64)
    pack.update(status="approved", data_origin="synthetic_test", reviewer="synthetic-auditor",
                reviewed_at="2026-01-03T00:00:00+00:00")
    # Baseline 10/15 minutes, candidate 5/10, one constructed pause.
    for index, (row, elapsed, paused) in enumerate(zip(pack["records"], [600, 300, 1200, 600], [0, 0, 300, 0])):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)
        row.update(editor="synthetic-editor", source_run_id="synthetic-" + row["arm"],
                   draft_sha256="b" * 64, edited_sha256="c" * 64,
                   blind_id=f"synthetic-blind-{index}", order_index=index + 1,
                   started_at=start.isoformat(), finished_at=(start + timedelta(seconds=elapsed)).isoformat(),
                   paused_seconds=paused, reviewer="synthetic-quality-reviewer",
                   reviewed_at="2026-01-02T00:00:00Z", meets_criterion=True)
    return pack


class EditingTimeTests(unittest.TestCase):
    def test_pending_gold_creates_unapproved_empty_form(self):
        gold = {"status": "pending_human_review", "cases": synthetic_records()["planned_cases"]}
        pack = make_template(gold, experiment_id="synthetic", criterion_version="v1",
                             criterion_text="A human must define this", gold_sha256="a" * 64)
        self.assertEqual(pack["status"], "pending_human_review")
        for row in pack["records"]:
            self.assertIsNone(row["started_at"])
            self.assertIsNone(row["paused_seconds"])
            self.assertIsNone(row["meets_criterion"])
        with self.assertRaises(ValueError):
            score_records(pack)

    def test_known_paired_averages_and_pause_arithmetic(self):
        result = score_records(synthetic_records())
        self.assertEqual(result["paired_case_count"], 2)
        self.assertEqual(result["data_origin"], "synthetic_test")
        self.assertEqual(result["arms"]["baseline"]["mean_minutes"], 12.5)
        self.assertEqual(result["arms"]["candidate"]["median_minutes"], 7.5)
        self.assertEqual(result["paired"]["mean_reduction_minutes"], 5)
        self.assertEqual(result["paired"]["relative_reduction_percent"], 40)
        self.assertEqual(result["paired"]["relative_reduction_denominator_seconds"], 1500)

    def test_missing_duplicate_and_unplanned_records_rejected(self):
        pack = synthetic_records()
        variants = [pack["records"][:-1], pack["records"] + [deepcopy(pack["records"][0])],
                    [pack["records"][0], pack["records"][0], *pack["records"][2:]]]
        for rows in variants:
            invalid = deepcopy(pack)
            invalid["records"] = rows
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                score_records(invalid)
        pack["records"][0]["case_id"] = "unplanned"
        with self.assertRaises(ValueError):
            score_records(pack)

    def test_group_hash_identity_and_criterion_rejected(self):
        for key, value in (("event_group_id", "wrong"), ("draft_sha256", "short"),
                           ("edited_sha256", "g" * 64), ("source_run_id", None),
                           ("criterion_version", "different")):
            pack = synthetic_records()
            pack["records"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                score_records(pack)

    def test_approval_signatures_and_completed_quality_required(self):
        for field, value in (("status", "pending_human_review"), ("reviewer", None),
                             ("reviewed_at", None), ("data_origin", None)):
            pack = synthetic_records()
            pack[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                score_records(pack)
        for field, value in (("reviewer", None), ("meets_criterion", False), ("editor", None),
                             ("order_index", None), ("blind_id", None)):
            pack = synthetic_records()
            pack["records"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                score_records(pack)

    def test_timezones_and_review_chronology_required(self):
        for field, value in (("started_at", "2026-01-01T00:00:00"),
                             ("finished_at", "not-a-date"), ("reviewed_at", "2025-12-31T00:00:00Z")):
            pack = synthetic_records()
            pack["records"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                score_records(pack)
        pack = synthetic_records()
        pack["records"][0]["started_at"] = "2026-01-01T08:00:00+08:00"
        self.assertEqual(score_records(pack)["arms"]["baseline"]["mean_minutes"], 12.5)

    def test_negative_elapsed_and_invalid_pause_rejected(self):
        for value in (None, -1, 601, float("inf"), float("nan"), True, "0"):
            pack = synthetic_records()
            pack["records"][0]["paused_seconds"] = value
            with self.subTest(paused=value), self.assertRaises(ValueError):
                score_records(pack)
        pack = synthetic_records()
        pack["records"][0]["finished_at"] = "2025-12-31T23:59:00Z"
        with self.assertRaises(ValueError):
            score_records(pack)

    def test_zero_baseline_has_no_relative_percentage(self):
        pack = synthetic_records()
        for row in pack["records"]:
            row.update(finished_at=row["started_at"], paused_seconds=0)
        result = score_records(pack)
        self.assertEqual(result["paired"]["mean_reduction_minutes"], 0)
        self.assertIsNone(result["paired"]["relative_reduction_percent"])

    def test_degradation_keeps_negative_reduction(self):
        pack = synthetic_records()
        for row in pack["records"]:
            if row["arm"] == "baseline":
                row["paused_seconds"] = (datetime.fromisoformat(row["finished_at"]) -
                                         datetime.fromisoformat(row["started_at"])).total_seconds()
        result = score_records(pack)
        self.assertLess(result["paired"]["mean_reduction_minutes"], 0)
        self.assertEqual(result["paired"]["worsened_pairs"], 2)

    def test_duplicate_plan_or_editor_order_rejected(self):
        pack = synthetic_records()
        pack["planned_cases"].append(deepcopy(pack["planned_cases"][0]))
        with self.assertRaises(ValueError):
            score_records(pack)
        pack = synthetic_records()
        pack["records"][1]["order_index"] = pack["records"][0]["order_index"]
        with self.assertRaises(ValueError):
            score_records(pack)

    def test_cli_never_overwrites_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            records, output = Path(folder) / "synthetic.json", Path(folder) / "synthetic-results.json"
            records.write_text(json.dumps(synthetic_records()), encoding="utf-8")
            main(["score", "--records", str(records), "--output", str(output)])
            original = output.read_bytes()
            with self.assertRaises(SystemExit) as error:
                main(["score", "--records", str(records), "--output", str(output)])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(output.read_bytes(), original)

    def test_template_cli_uses_real_file_hash_but_no_human_times(self):
        with tempfile.TemporaryDirectory() as folder:
            gold, output = Path(folder) / "synthetic-gold.json", Path(folder) / "synthetic-template.json"
            gold.write_text(json.dumps({"status": "pending_human_review",
                                        "cases": synthetic_records()["planned_cases"]}), encoding="utf-8")
            main(["template", "--gold-file", str(gold), "--experiment-id", "synthetic",
                  "--criterion-version", "v1", "--criterion-text", "Synthetic criterion",
                  "--output", str(output)])
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(result["gold_sha256"]), 64)
            self.assertEqual(result["status"], "pending_human_review")
            self.assertTrue(all(row["editor"] is None for row in result["records"]))


if __name__ == "__main__":
    unittest.main()
