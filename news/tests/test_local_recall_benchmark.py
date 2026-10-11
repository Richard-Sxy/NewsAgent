"""Verify article-level candidate completion and independent baseline fusion."""
import unittest

from experiments.local_recall_benchmark import (
    BM25, hnsw_news, lexical_tokens, news_from_scores, rrf_news,
)


class FakeIndex:
    def __init__(self):
        self.calls = []
        self.ef = []

    def set_ef(self, ef):
        self.ef.append(ef)

    def knn_query(self, vector, k, num_threads):
        self.calls.append(k)
        return [list(range(k))], [[0] * k]


class BenchmarkTests(unittest.TestCase):
    def test_ann_expands_duplicate_chunks_to_distinct_news(self):
        chunks = [{"news_id": "a"}] * 11 + [{"news_id": "b"}, {"news_id": "c"}]
        index = FakeIndex()
        news, labels, count = hnsw_news(index, [0.1], chunks, 3, 8)
        self.assertEqual(news, ["a", "b", "c"])
        self.assertEqual(index.calls, [12, 13])
        self.assertEqual(count, 13)
        self.assertEqual(len(labels), 13)

    def test_ann_stops_when_corpus_has_fewer_than_k_news(self):
        index = FakeIndex()
        news, _, count = hnsw_news(index, [0.1], [{"news_id": "a"}] * 5, 10, 64)
        self.assertEqual(news, ["a"])
        self.assertEqual(count, 5)

    def test_exact_ranks_news_by_best_chunk_and_stable_tie(self):
        chunks = [
            {"news_id": "a", "chunk_id": "z"},
            {"news_id": "a", "chunk_id": "x"},
            {"news_id": "b", "chunk_id": "b"},
            {"news_id": "c", "chunk_id": "c"},
        ]
        self.assertEqual(news_from_scores([0.1, 0.9, 0.9, 0.5], chunks, 3), ["b", "a", "c"])

    def test_rrf_favors_agreement_and_deduplicates_each_list(self):
        self.assertEqual(rrf_news([["a", "a", "c"], ["b", "c"]], 3)[0], "c")

    def test_bm25_uses_query_text_only_and_tracks_chinese_terms(self):
        model = BM25(["新能源电池产业", "羽毛球比赛夺冠"])
        scores = list(model.scores("电池产业"))
        self.assertGreater(scores[0], scores[1])
        self.assertIn("电池", lexical_tokens("电池API"))
        self.assertIn("api", lexical_tokens("电池API"))


if __name__ == "__main__":
    unittest.main()
