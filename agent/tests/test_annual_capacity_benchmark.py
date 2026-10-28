"""Independent small-array contracts for annual capacity ANN measurements.

No Faiss, model, network or generated annual data is required.
"""
from __future__ import annotations

import unittest
import numpy as np

from evaluation.annual_capacity_benchmark import (
    ann_recall, exact_top_news, fuse_candidates, percentiles,
)


def rows(scores, ids):
    return np.asarray(scores, dtype=np.float32), np.asarray(ids, dtype=np.int64)


def naive_top_news(vectors_by_tier, queries, chunks_per_document, k):
    """A deliberately simple per-query/per-document reference, with no top-k helper."""
    results = []
    for query in queries:
        documents = []
        news_key = 0
        for tier in ("hot", "warm", "cold"):
            vectors = vectors_by_tier[tier]
            for start in range(0, len(vectors), chunks_per_document):
                scores = [
                    sum(float(a) * float(b) for a, b in zip(query, vector))
                    for vector in vectors[start:start + chunks_per_document]
                ]
                documents.append((news_key, max(scores)))
                news_key += 1
        documents.sort(key=lambda item: (-item[1], item[0]))
        results.append([news_id for news_id, _ in documents[:k]])
    return np.asarray(results, dtype=np.int64)


class CandidateFusionContracts(unittest.TestCase):
    def metadata(self):
        return {"hot": 10, "warm": 10, "cold": 10}, {"hot": 0, "warm": 2, "cold": 4}

    def test_multiple_chunks_use_best_article_score_and_evidence(self):
        counts, offsets = self.metadata()
        result = fuse_candidates(
            {"hot": rows([.3, .95, .7], [0, 4, 5]),
             "warm": rows([], []), "cold": rows([], [])},
            counts, offsets, chunks_per_document=5, k=10)
        self.assertEqual([item["news_key"] for item in result], [0, 1])
        self.assertAlmostEqual(result[0]["score"], .95, places=6)
        self.assertEqual(int(result[0]["chunk_id"]), 4)
        self.assertEqual(result[0]["tier"], "hot")

    def test_negative_and_out_of_range_ids_never_serve(self):
        counts, offsets = self.metadata()
        result = fuse_candidates(
            {"hot": rows([.99, .98, .6, .5], [-1, 10, 0, 9]),
             "warm": rows([], []), "cold": rows([], [])},
            counts, offsets, chunks_per_document=5, k=10)
        self.assertEqual([item["news_key"] for item in result], [0, 1])
        self.assertEqual([int(item["chunk_id"]) for item in result], [0, 9])
        self.assertTrue(all(item["score"] < .9 for item in result))

    def test_global_similarity_can_put_cold_news_first(self):
        counts, offsets = self.metadata()
        result = fuse_candidates(
            {"hot": rows([.5], [0]), "warm": rows([.7], [0]), "cold": rows([.99], [0])},
            counts, offsets, chunks_per_document=5, k=3)
        self.assertEqual([item["news_key"] for item in result], [4, 2, 0])
        self.assertEqual([item["tier"] for item in result], ["cold", "warm", "hot"])

    def test_score_ties_are_stable_across_tiers_and_chunk_order(self):
        counts, offsets = self.metadata()
        result = fuse_candidates(
            {"cold": rows([.8], [0]), "warm": rows([.8], [0]),
             "hot": rows([.8, .8, .8], [4, 2, 5])},
            counts, offsets, chunks_per_document=5, k=4)
        self.assertEqual([item["news_key"] for item in result], [0, 1, 2, 4])
        self.assertEqual(int(result[0]["chunk_id"]), 2)

    def test_nonfinite_similarity_is_not_silently_ranked(self):
        counts, offsets = self.metadata()
        for score in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(score=score), self.assertRaises(ValueError):
                fuse_candidates(
                    {"hot": rows([score], [0]), "warm": rows([], []), "cold": rows([], [])},
                    counts, offsets, chunks_per_document=5, k=1)

    def test_empty_tier_cannot_invent_results(self):
        counts = {"hot": 0, "warm": 0, "cold": 5}
        offsets = {"hot": 0, "warm": 0, "cold": 0}
        result = fuse_candidates(
            {"hot": rows([.99], [0]), "warm": rows([], []), "cold": rows([.4], [4])},
            counts, offsets, chunks_per_document=5, k=10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tier"], "cold")
        self.assertEqual(result[0]["news_key"], 0)


class ExactGroundTruthContracts(unittest.TestCase):
    def test_blocked_search_matches_independent_naive_reference(self):
        rng = np.random.default_rng(20261028)
        vectors = {
            "hot": rng.standard_normal((15, 7)).astype(np.float32),
            "warm": rng.standard_normal((20, 7)).astype(np.float32),
            "cold": rng.standard_normal((25, 7)).astype(np.float32),
        }
        queries = rng.standard_normal((4, 7)).astype(np.float32)
        expected = naive_top_news(vectors, queries, 5, 8)
        for block_docs in (1, 2, 4):
            with self.subTest(block_docs=block_docs):
                actual = exact_top_news(vectors, queries, chunks_per_document=5,
                                        k=8, block_docs=block_docs)
                self.assertEqual(actual.dtype, np.dtype("int64"))
                self.assertEqual(actual.shape, (4, 8))
                np.testing.assert_array_equal(actual, expected)

    def test_equal_scores_use_global_news_keys_at_topk_boundary(self):
        vectors = {
            "hot": np.zeros((10, 3), dtype=np.float32),
            "warm": np.zeros((10, 3), dtype=np.float32),
            "cold": np.zeros((10, 3), dtype=np.float32),
        }
        result = exact_top_news(vectors, np.asarray([[1., 2., 3.]], dtype=np.float32),
                                chunks_per_document=5, k=4, block_docs=1)
        np.testing.assert_array_equal(result, np.asarray([[0, 1, 2, 3]], dtype=np.int64))

    def test_best_of_many_chunks_counts_one_document(self):
        vectors = {
            "hot": np.asarray([[9., 0.]] * 5 + [[8., 0.]] * 5, dtype=np.float32),
            "warm": np.asarray([[7., 0.]] * 5, dtype=np.float32),
            "cold": np.asarray([[10., 0.]] * 5, dtype=np.float32),
        }
        result = exact_top_news(vectors, np.asarray([[1., 0.]], dtype=np.float32),
                                chunks_per_document=5, k=3, block_docs=1)
        np.testing.assert_array_equal(result, np.asarray([[3, 0, 1]], dtype=np.int64))


class AnnRecallContracts(unittest.TestCase):
    def test_query_average_has_true_k_denominator_and_no_duplicate_credit(self):
        exact = np.asarray([[1, 2, 3], [4, 5, 6]], dtype=np.int64)
        approx = np.asarray([[1, 1, 8], [4, 6, 9]], dtype=np.int64)
        self.assertAlmostEqual(ann_recall(exact, approx), .5)

    def test_full_and_zero_overlap(self):
        exact = np.asarray([[1, 2], [3, 4]], dtype=np.int64)
        self.assertEqual(ann_recall(exact, exact[:, ::-1]), 1.)
        self.assertEqual(ann_recall(exact, exact + 20), 0.)

    def test_missing_queries_or_cutoff_mismatch_is_an_error(self):
        exact = np.asarray([[1, 2, 3], [4, 5, 6]], dtype=np.int64)
        for approx in (exact[:1], exact[:, :2]):
            with self.subTest(shape=approx.shape), self.assertRaises(ValueError):
                ann_recall(exact, approx)


class PercentileContracts(unittest.TestCase):
    def test_empty_sample_is_unknown_not_zero_latency(self):
        result = percentiles([])
        self.assertEqual(result["count"], 0)
        for key in ("p50", "p95", "p99"):
            self.assertIsNone(result[key])

    def test_one_sample(self):
        result = percentiles([7.])
        self.assertEqual(result["count"], 1)
        self.assertEqual([result[key] for key in ("p50", "p95", "p99")], [7., 7., 7.])

    def test_two_samples_use_r7_linear_interpolation(self):
        result = percentiles([20., 10.])
        self.assertEqual(result["count"], 2)
        self.assertAlmostEqual(result["p50"], 15.)
        self.assertAlmostEqual(result["p95"], 19.5)
        self.assertAlmostEqual(result["p99"], 19.9)

    def test_unsorted_samples(self):
        result = percentiles([100., 0., 50., 25., 75.])
        self.assertEqual(result["count"], 5)
        self.assertAlmostEqual(result["p50"], 50.)
        self.assertAlmostEqual(result["p95"], 95.)
        self.assertAlmostEqual(result["p99"], 99.)


if __name__ == "__main__":
    unittest.main()
