"""Known-answer checks for retrieval evaluation; only stdlib is required."""

import math
import unittest

from experiments.recall_metrics import (
    aggregate_metrics,
    ann_overlap,
    query_metrics,
    unique_ranked,
)


class RecallMetricsTests(unittest.TestCase):
    def test_multiple_labels_distinguish_hit_rate_from_recall(self):
        result = query_metrics(["a", "other"], {"a": 1, "b": 1}, [1, 2])
        self.assertEqual(result[1]["hit_rate"], 1.0)
        self.assertEqual(result[1]["recall"], 0.5)
        self.assertEqual(result[1]["precision"], 1.0)
        self.assertEqual(result[2]["precision"], 0.5)

    def test_duplicate_chunks_do_not_consume_news_rank(self):
        self.assertEqual(unique_ranked(["a", "a", "b", "a", "c"], 3), ["a", "b", "c"])
        result = query_metrics(["other", "other", "a", "a"], {"a": 1}, [1, 2])
        self.assertEqual(result[1]["mrr"], 0.0)
        self.assertEqual(result[2]["mrr"], 0.5)
        self.assertEqual(result[2]["recall"], 1.0)

    def test_mrr_is_truncated_at_each_k(self):
        result = query_metrics(["x", "y", "a"], {"a": 1}, [1, 2, 3, 10])
        self.assertEqual(result[2]["mrr"], 0.0)
        self.assertAlmostEqual(result[3]["mrr"], 1 / 3)
        self.assertAlmostEqual(result[10]["mrr"], 1 / 3)
        self.assertEqual(result[10]["precision"], 0.1)

    def test_graded_ndcg_uses_exponential_gain(self):
        labels = {"high": 3, "low": 1}
        ideal = query_metrics(["high", "low"], labels, [1, 2])
        reversed_order = query_metrics(["low", "high"], labels, [1, 2])
        self.assertAlmostEqual(ideal[1]["ndcg"], 1.0)
        self.assertAlmostEqual(ideal[2]["ndcg"], 1.0)
        self.assertAlmostEqual(reversed_order[1]["ndcg"], 1 / 7)
        self.assertAlmostEqual(reversed_order[2]["ndcg"],
                               (1 + 7 / math.log2(3)) / (7 + 1 / math.log2(3)))

    def test_out_of_corpus_label_stays_in_denominator(self):
        result = query_metrics(["a"], {"a": 1, "missing_from_corpus": 1}, [5])
        self.assertEqual(result[5]["recall"], 0.5)
        self.assertEqual(result[5]["precision"], 0.2)

    def test_empty_ranked_results_are_zero(self):
        result = query_metrics([], {"a": 1}, [1, 10])
        self.assertTrue(all(value == 0 for metrics in result.values() for value in metrics.values()))

    def test_large_and_tiny_positive_grades_have_finite_ndcg(self):
        for grades in ({"a": 2000.0, "b": 1000.0}, {"a": 1e-200, "b": 2e-200}):
            with self.subTest(grades=grades):
                ordered = sorted(grades, key=grades.get, reverse=True)
                self.assertAlmostEqual(query_metrics(ordered, grades, [2])[2]["ndcg"], 1.0)

    def test_empty_labels_and_invalid_grades_are_rejected(self):
        with self.assertRaises(ValueError):
            query_metrics(["a"], {}, [1])
        for grade in (0, -1, float("nan"), float("inf"), -float("inf"), True, "1"):
            with self.subTest(grade=grade), self.assertRaises(ValueError):
                query_metrics(["a"], {"a": grade}, [1])

    def test_invalid_k_is_rejected_by_all_entry_points(self):
        for k in (0, -1, 1.5, True, "2"):
            for fn in (
                lambda: unique_ranked(["a"], k),
                lambda: query_metrics(["a"], {"a": 1}, [k]),
                lambda: aggregate_metrics([], [k]),
                lambda: ann_overlap(["a"], ["a"], k),
            ):
                with self.subTest(k=k, fn=fn), self.assertRaises(ValueError):
                    fn()
        with self.assertRaises(ValueError):
            query_metrics(["a"], {"a": 1}, [])

    def test_cutoffs_may_exceed_result_count_or_repeat(self):
        result = query_metrics(["a"], {"a": 1}, [1, 5, 1])
        self.assertEqual(list(result), [1, 5])
        self.assertEqual(result[5]["recall"], 1.0)
        self.assertEqual(result[5]["precision"], 0.2)
        self.assertEqual(result[5]["ndcg"], 1.0)

    def test_invalid_news_ids_are_rejected(self):
        for news_id in (None, "", " ", 10):
            with self.subTest(news_id=news_id), self.assertRaises(ValueError):
                unique_ranked([news_id], 1)

    def test_macro_average_weights_queries_equally(self):
        rows = [query_metrics(["a"], {"a": 1}, [1]),
                query_metrics(["b"], {"b": 1, "c": 1, "d": 1}, [1])]
        result = aggregate_metrics(iter(rows), [1])
        self.assertEqual(result[1]["hit_rate"], 1.0)
        self.assertAlmostEqual(result[1]["recall"], 2 / 3)

    def test_aggregate_rejects_empty_incomplete_or_invalid_rows(self):
        with self.assertRaises(ValueError):
            aggregate_metrics([], [1])
        valid = query_metrics(["a"], {"a": 1}, [1])
        with self.assertRaises(ValueError):
            aggregate_metrics([valid], [2])
        for value in (float("nan"), float("inf"), -0.1, 1.1, None, True):
            invalid = {1: {**valid[1], "recall": value}}
            with self.subTest(value=value), self.assertRaises(ValueError):
                aggregate_metrics([invalid], [1])
        with self.assertRaises(ValueError):
            aggregate_metrics([{1: {"recall": 1.0}}], [1])

    def test_ann_overlap_is_separate_from_relevance_metrics(self):
        self.assertEqual(ann_overlap(["a", "a", "c"], ["a", "a", "b"], 2), 0.5)
        self.assertEqual(ann_overlap(["a"], ["a"], 10), 1.0)
        self.assertEqual(ann_overlap([], ["a"], 2), 0.0)
        with self.assertRaises(ValueError):
            ann_overlap(["a"], [], 2)


if __name__ == "__main__":
    unittest.main()
