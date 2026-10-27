"""Behavior tests for the local human-review store (stdlib unittest)."""
import copy
import json
import math
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.modules.retrieval_lab.metrics import calculate_metrics
from app.modules.retrieval_lab.store import FrozenRunConflict, RetrievalLabStore, RevisionConflict


def example_run(run_id="r1", count=3):
    return {"run_id": run_id, "query_id": "q1", "query": "测试事件的最新进展？", "category": "科技",
            "versions": {"corpus": "corpus-1", "model": "model-1", "strategy": "strategy-1"},
            "trace": {"cold": {"count": count}},
            "candidates": [{"news_id": "n{}".format(i), "rank": i, "score": 0.99 / i,
                            "title": "新闻{}".format(i), "body": "正文<script>仅是数据</script>",
                            "url": "https://example.com/{}".format(i), "tier": "cold"}
                           for i in range(1, count + 1)]}


def judged_candidate(rank, label=None):
    return {"rank": rank, "judgment": {"label": label,
            "status": "unjudged" if label is None else ("unable" if label == "unsure" else "judged")}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "lab.sqlite3"
        self.store = RetrievalLabStore(self.path)
        self.run = example_run()
        self.store.put_run(self.run)

    def tearDown(self):
        self.directory.cleanup()

    def test_initial_candidates_have_no_automatic_gold(self):
        result = self.store.get_run("r1")
        self.assertEqual(result["progress"]["judged"], 0)
        self.assertEqual(result["candidates"][0]["body"], self.run["candidates"][0]["body"])
        self.assertEqual(result["trace"], self.run["trace"])
        self.assertTrue(all(c["judgment"]["label"] is None and c["revision"] == 0 for c in result["candidates"]))
        self.assertIsNone(result["metrics"]["precision_at_k"])
        self.assertIsNone(result["metrics"]["accuracy"])

    def test_human_judgment_persists_after_reopen(self):
        saved = self.store.save_judgment("r1", "n1", label="relevant", expected_revision=0, reviewer="测试员", notes="直接回答")
        reopened = RetrievalLabStore(self.path).get_run("r1")
        self.assertEqual(saved["judgment"], reopened["candidates"][0]["judgment"])
        self.assertEqual(reopened["progress"]["judged"], 1)
        self.assertEqual(saved["judgment"]["grade"], 2)
        self.assertEqual(saved["judgment"]["revision"], 1)

    def test_unsure_is_explicit_and_not_negative(self):
        self.store.save_judgment("r1", "n1", label="unsure", expected_revision=0)
        result = self.store.get_run("r1")
        self.assertEqual(result["progress"]["unable"], 1)
        self.assertEqual(result["progress"]["judged"], 0)
        self.assertEqual(result["progress"]["remaining"], 2)
        self.assertIsNone(result["candidates"][0]["judgment"]["grade"])
        self.assertIsNone(self.store.metrics("r1", 1)["precision_at_k"])

    def test_revision_conflict_does_not_overwrite_or_audit(self):
        saved = self.store.save_judgment("r1", "n1", label="relevant", expected_revision=0)
        with self.assertRaises(RevisionConflict) as caught:
            self.store.save_judgment("r1", "n1", label="irrelevant", expected_revision=0)
        self.assertEqual(caught.exception.current, saved["judgment"])
        self.assertEqual(len(self.store.judgment_history("r1", "n1")), 1)
        self.assertEqual(self.store.get_run("r1")["candidates"][0]["judgment"]["label"], "relevant")

    def test_revision_audit_retains_old_human_labels(self):
        self.store.save_judgment("r1", "n1", label="relevant", expected_revision=0, reviewer="a")
        self.store.save_judgment("r1", "n1", label="partial", expected_revision=1, reviewer="b", notes="补充背景")
        history = self.store.judgment_history("r1", "n1")
        self.assertEqual([h["label"] for h in history], ["relevant", "partial"])
        self.assertEqual([h["reviewer"] for h in history], ["a", "b"])
        self.assertEqual([h["revision"] for h in history], [1, 2])

    def test_concurrent_writers_only_one_wins_same_revision(self):
        def write(label):
            try:
                self.store.save_judgment("r1", "n1", label=label, expected_revision=0)
                return "saved"
            except RevisionConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, ["relevant", "irrelevant"]))
        self.assertEqual(sorted(results), ["conflict", "saved"])
        self.assertEqual(len(self.store.judgment_history("r1", "n1")), 1)

    def test_unknown_run_candidate_cannot_be_reviewed(self):
        for run_id, news_id in [("missing", "n1"), ("r1", "absent")]:
            with self.subTest(run=run_id, news=news_id), self.assertRaises(KeyError):
                self.store.save_judgment(run_id, news_id, label="relevant", expected_revision=0)
        self.assertEqual(self.store.export_data()["judgment_audit"], [])

    def test_repeated_identical_freeze_preserves_judgment(self):
        self.store.save_judgment("r1", "n1", label="relevant", expected_revision=0)
        result = self.store.put_run(self.run)
        self.assertEqual(result["candidates"][0]["revision"], 1)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_changed_frozen_run_is_rejected(self):
        modified = copy.deepcopy(self.run)
        modified["candidates"][0]["body"] = "different"
        with self.assertRaises(FrozenRunConflict):
            self.store.put_run(modified)
        self.assertEqual(self.store.get_run("r1")["candidates"][0]["body"], self.run["candidates"][0]["body"])

    def test_labels_do_not_leak_across_runs(self):
        self.store.put_run(example_run("r2"))
        self.store.save_judgment("r1", "n1", label="relevant", expected_revision=0)
        self.assertIsNone(self.store.get_run("r2")["candidates"][0]["judgment"]["label"])

    def test_manifest_is_immutable_and_exported(self):
        self.store.set_manifest({"corpus": "c1", "source_hash": "123"})
        self.store.set_manifest({"source_hash": "123", "corpus": "c1"})
        with self.assertRaises(FrozenRunConflict):
            self.store.set_manifest({"corpus": "c2"})
        self.assertEqual(self.store.export_data()["manifest"]["corpus"], "c1")

    def test_run_validation_rejects_duplicates_gaps_missing_versions_and_labels(self):
        invalid = []
        duplicate = copy.deepcopy(self.run); duplicate["candidates"][1]["news_id"] = "n1"; invalid.append(duplicate)
        gap = copy.deepcopy(self.run); gap["candidates"][1]["rank"] = 4; invalid.append(gap)
        missing = copy.deepcopy(self.run); del missing["versions"]["model"]; invalid.append(missing)
        label = copy.deepcopy(self.run); label["candidates"][0]["judgment"] = {"label": "relevant"}; invalid.append(label)
        nonfinite = copy.deepcopy(self.run); nonfinite["candidates"][0]["score"] = float("nan"); invalid.append(nonfinite)
        for run in invalid:
            with self.subTest(run=run), self.assertRaises(ValueError):
                self.store.put_run(run)

    def test_judgment_validation_keeps_history_empty(self):
        for overrides in [{"label": "gold"}, {"label": []}, {"expected_revision": -1}, {"expected_revision": True},
                          {"reviewer": ""}, {"notes": 42}]:
            kwargs = {"label": "relevant", "expected_revision": 0}; kwargs.update(overrides)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.save_judgment("r1", "n1", **kwargs)
        self.assertEqual(self.store.judgment_history("r1", "n1"), [])

    def test_tampered_run_hash_is_detected(self):
        with sqlite3.connect(str(self.path)) as connection:
            connection.execute("UPDATE lab_runs SET payload=? WHERE run_id='r1'", (json.dumps(example_run("tampered")),))
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.store.get_run("r1")

    def test_tampered_manifest_is_rejected_on_read_and_export(self):
        self.store.set_manifest({"corpus": "c1"})
        with sqlite3.connect(str(self.path)) as connection:
            connection.execute("UPDATE lab_manifest SET payload=?", ('{"corpus":"forged"}',))
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.store.get_manifest()
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.store.export_data()

    def test_export_contains_versions_labels_and_audit_but_no_promoted_gold(self):
        self.store.save_judgment("r1", "n1", label="partial", expected_revision=0)
        exported = self.store.export_data()
        self.assertEqual(exported["review_scope"], "single_human_review_not_promoted_gold")
        self.assertEqual(exported["runs"][0]["versions"], self.run["versions"])
        self.assertEqual(exported["judgment_audit"][0]["judgment"]["grade"], 1)
        self.assertEqual(len(exported["runs"][0]["snapshot_sha256"]), 64)
        json.dumps(exported, allow_nan=False)


class MetricsTests(unittest.TestCase):
    def test_no_judgments_yields_no_quality_claims(self):
        m = calculate_metrics([judged_candidate(1), judged_candidate(2)], 2)
        self.assertIsNone(m["precision_at_k"])
        self.assertIsNone(m["pool_ndcg_at_k"])
        self.assertIsNone(m["recall_at_k"])

    def test_unjudged_not_counted_as_negative(self):
        m = calculate_metrics([judged_candidate(1, "relevant"), judged_candidate(2)], 2)
        self.assertIsNone(m["precision_at_k"])
        self.assertEqual(m["judged_precision_at_k"], 1.0)
        self.assertEqual(m["judged_coverage_at_k"], 0.5)

    def test_top_k_complete_precision_can_show_before_pool_ndcg(self):
        m = calculate_metrics([judged_candidate(1, "relevant"), judged_candidate(2, "partial"), judged_candidate(3)], 2)
        self.assertEqual(m["precision_at_k"], 0.5)
        self.assertIsNone(m["pool_ndcg_at_k"])
        self.assertEqual(m["partial_at_k"], 1)

    def test_pool_ndcg_uses_graded_human_judgments(self):
        m = calculate_metrics([judged_candidate(1, "partial"), judged_candidate(2, "irrelevant"), judged_candidate(3, "relevant")], 2)
        self.assertEqual(m["precision_at_k"], 0.0)
        self.assertAlmostEqual(m["pool_ndcg_at_k"], 1 / (3 + 1 / math.log2(3)))
        self.assertIsNone(m["recall_at_k"])

    def test_unsure_keeps_ndcg_ineligible(self):
        m = calculate_metrics([judged_candidate(1, "relevant"), judged_candidate(2, "unsure")], 1)
        self.assertEqual(m["precision_at_k"], 1.0)
        self.assertIsNone(m["pool_ndcg_at_k"])
        self.assertFalse(m["pool_fully_judged"])

    def test_no_relevant_pool_ndcg_is_undefined(self):
        m = calculate_metrics([judged_candidate(1, "irrelevant")], 1)
        self.assertEqual(m["precision_at_k"], 0.0)
        self.assertIsNone(m["pool_ndcg_at_k"])
        self.assertEqual(m["pool_ndcg_reason"], "no_relevant_or_partial_document_in_pool")

    def test_fewer_than_k_does_not_silently_change_denominator(self):
        m = calculate_metrics([judged_candidate(1, "relevant")], 10)
        self.assertIsNone(m["precision_at_k"])
        self.assertIsNone(m["pool_ndcg_at_k"])
        self.assertEqual(m["returned_at_k"], 1)

    def test_invalid_k(self):
        for k in (0, -1, True, 1.5):
            with self.subTest(k=k), self.assertRaises(ValueError):
                calculate_metrics([], k)


if __name__ == "__main__":
    unittest.main()
