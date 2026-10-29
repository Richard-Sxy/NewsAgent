"""Synthetic fixture tests for source freezing, not model quality evidence."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import random
import tempfile
import unittest

from evaluation.prepare_writing_scale import build_dataset, group_sources, load_sources, sha


def fixture_rows(groups=3):
    rows = []
    categories = ["股票", "科技", "体育", "游戏"]
    for group in range(groups):
        for item in range(2):
            rng = random.Random(1000 + group * 17 + item)
            body = "".join(chr(0x4E00 + rng.randrange(3000)) for _ in range(500)) + "。合成数量12人。"
            rows.append({"news_id": "synthetic-%s-%s" % (group, item),
                "body": body, "body_sha256": sha(body), "title": "合成测试事件%s来源%s" % (group, item),
                "url": "https://example.invalid/group-%s/item-%s" % (group, item),
                "event_ids": ["synthetic-event-%s" % group], "categories": [categories[group % len(categories)]],
                "publication_date": "2026-10-0%s" % (item + 1), "publish_time": None,
                "published_at_original": "2026-10-0%s" % (item + 1), "date_precision": "day",
                "source_evidence": {"body_sha256": sha(body)}})
    return rows


class WritingScaleDatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "sources.jsonl"
        self.rows = fixture_rows()
        self.save(self.rows)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, rows):
        self.source.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def build(self, name="dataset", **kwargs):
        args = {"source_path": self.source, "chains_path": None, "out": self.root / name,
                "evaluation_groups": 2, "development_groups": 1, "development_reference": None}
        args.update(kwargs)
        return build_dataset(**args)

    def test_full_sources_two_distinct_tasks_disjoint_groups_and_pending_gold(self):
        manifest = self.build()
        out = self.root / "dataset"
        inputs = [json.loads(line) for line in (out / "inputs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(Counter(row["split"] for row in inputs), {"evaluation": 4, "development": 2})
        self.assertEqual(manifest["evaluation_groups"], 2)
        self.assertEqual(manifest["development_groups"], 1)
        by_id = {row["news_id"]: row for row in self.rows}
        seen = {}
        for row in inputs:
            self.assertEqual(set(row), {"case_id", "event_group_id", "split", "input"})
            self.assertEqual(set(row["input"]), {"topic", "requirements"})
            requirements = row["input"]["requirements"]
            self.assertEqual(set(requirements), {"language", "audience", "target_word_count", "max_sections",
                                                "as_of", "task_description", "source_documents"})
            self.assertEqual(requirements["target_word_count"], 300)
            self.assertEqual(len(requirements["source_documents"]), 2)
            for source in requirements["source_documents"]:
                self.assertEqual(set(source), {"source_id", "news_id", "url", "title", "published_at", "content"})
                self.assertEqual(source["content"], by_id[source["news_id"]]["body"])
                self.assertEqual(source["published_at"], by_id[source["news_id"]]["publication_date"])
                identity = source["news_id"]
                if identity in seen:
                    self.assertEqual(seen[identity], (row["event_group_id"], row["split"]))
                seen[identity] = (row["event_group_id"], row["split"])
        for group in set(row["event_group_id"] for row in inputs):
            tasks = [row for row in inputs if row["event_group_id"] == group]
            self.assertEqual(len(tasks), 2)
            self.assertNotEqual(tasks[0]["input"]["requirements"]["task_description"],
                                tasks[1]["input"]["requirements"]["task_description"])
        gold = json.loads((out / "private/gold-review.draft.json").read_text(encoding="utf-8"))
        self.assertFalse(gold["approved"])
        self.assertFalse(gold["model_input_allowed"])
        for case in gold["cases"]:
            self.assertFalse(case["approved"])
            self.assertEqual(case["status"], "draft_pending_human_review")
            self.assertIsNone(case["numeric_checks"])
            self.assertIsNone(case["reviewer"])
        for group in gold["groups"]:
            self.assertIsNone(group["event_boundary_verified"])
            for source in group["sources"]:
                for number in source["numeric_candidates"]:
                    self.assertEqual(source["content"][number["start"]:number["end"]], number["text"])
                    self.assertIsNone(number["unit"])
                    self.assertIsNone(number["human_verified"])
        subset = json.loads((out / "editing-subset.json").read_text())
        self.assertEqual(len(subset["case_ids"]), 2)
        self.assertTrue(set(subset["case_ids"]) <= {row["case_id"] for row in inputs if row["split"] == "evaluation"})
        self.assertIn("pre_generation", subset["selection"])
        self.assertIsNone(subset["human_timings"])
        review = (out / "human-review.md").read_text(encoding="utf-8")
        for row in self.rows:
            self.assertIn(row["body"], review)

    def test_deterministic_before_generation_and_sha_manifest(self):
        first, second = self.build("one"), self.build("two")
        self.assertEqual(first, second)
        for relative, expected in first["files_sha256"].items():
            import hashlib
            self.assertEqual(hashlib.sha256((self.root / "one" / relative).read_bytes()).hexdigest(), expected)

    def test_existing_output_is_preserved(self):
        out = self.root / "dataset"
        out.mkdir()
        sentinel = out / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(sentinel.read_text(), "keep")

    def test_insufficient_groups_do_not_create_output_or_fake_tasks(self):
        with self.assertRaisesRegex(ValueError, "only 3 eligible"):
            self.build(evaluation_groups=50)
        self.assertFalse((self.root / "dataset").exists())

    def test_short_and_long_bodies_are_excluded_without_truncation(self):
        self.rows[0]["body"] = "短文。"
        self.rows[0]["body_sha256"] = sha(self.rows[0]["body"])
        self.rows[2]["body"] = "甲" * 5001
        self.rows[2]["body_sha256"] = sha(self.rows[2]["body"])
        self.save(self.rows)
        with self.assertRaisesRegex(ValueError, "only 1 eligible"):
            self.build()
        self.assertFalse((self.root / "dataset").exists())

    def test_shared_body_or_news_unions_event_ids_before_split(self):
        copied = dict(self.rows[0])
        copied["event_ids"] = ["synthetic-event-1"]
        rows = self.rows + [copied]
        groups, audit = group_sources(rows)
        self.assertEqual(len(groups), 2)
        self.assertTrue(audit["exact_unions"])
        merged = next(group for group in groups if len(group["event_ids"]) == 2)
        self.assertEqual(set(merged["event_ids"]), {"synthetic-event-0", "synthetic-event-1"})

    def test_near_lexical_copy_unions_groups(self):
        self.rows[2]["body"] = self.rows[0]["body"] + "补充。"
        self.rows[2]["body_sha256"] = sha(self.rows[2]["body"])
        groups, audit = group_sources(self.rows)
        self.assertEqual(len(groups), 2)
        self.assertTrue(audit["near_duplicate_unions"])

    def test_conflicting_news_id_and_hash_mismatch_are_rejected(self):
        self.rows[1]["news_id"] = self.rows[0]["news_id"]
        self.save(self.rows)
        with self.assertRaisesRegex(ValueError, "conflicting body"):
            load_sources(self.source)
        self.rows = fixture_rows()
        self.rows[0]["body_sha256"] = "0" * 64
        self.save(self.rows)
        with self.assertRaisesRegex(ValueError, "body_sha256 mismatch"):
            load_sources(self.source)

    def test_old_development_source_forces_whole_group_out_of_evaluation(self):
        reference = self.root / "old-development.jsonl"
        reference.write_text(json.dumps({"split": "development", "input": {"requirements": {"source_documents": [
            {"news_id": self.rows[0]["news_id"], "url": self.rows[0]["url"], "content": self.rows[0]["body"]}]}}}),
            encoding="utf-8")
        manifest = self.build(development_reference=reference)
        self.assertEqual(manifest["group_splits"]["synthetic-event-0"], "development")

    def test_unsupported_and_not_applicable_are_not_reported_as_passes(self):
        for row in self.rows:
            if row["event_ids"] == ["synthetic-event-0"]:
                row["body"] = row["body"].split("。")[0] + "。"
                row["body_sha256"] = sha(row["body"])
        self.save(self.rows)
        manifest = self.build()
        self.assertEqual(manifest["metric_applicability_counts"], {"not_applicable": 2, "unsupported": 4})
        self.assertFalse(any("pass" in item for item in manifest["case_metadata"].values()))
        self.assertTrue(all(item["matched_metric_count"] == 0 for item in manifest["case_metadata"].values()))

    def test_available_development_count_is_recorded_honestly(self):
        manifest = self.build(evaluation_groups=2, development_groups=10)
        self.assertEqual(manifest["development_groups"], 1)
        self.assertTrue(any("requested 10 was not fabricated" in item for item in manifest["limitations"]))


if __name__ == "__main__":
    unittest.main()
