"""Structural checks for offline token-window auditing; no model/network needed."""

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from tools.audit_index_tokenization import audit_body_coverage, audit_rows, summarize_counts, uncovered_intervals


class FakeTokenizer:
    truncation_side = "right"
    is_fast = True

    def __call__(self, texts, *, truncation, max_length=None, return_offsets_mapping=False, **_kwargs):
        rows = [[101, *range(len(text)), 102] for text in texts]
        offsets = [[(0, 0), *[(index, index + 1) for index in range(len(text))], (0, 0)] for text in texts]
        if truncation:
            rows = [row[:max_length - 1] + [row[-1]] if len(row) > max_length else row for row in rows]
            offsets = [row[:max_length - 1] + [row[-1]] if len(row) > max_length else row for row in offsets]
        return {"input_ids": rows, **({"offset_mapping": offsets} if return_offsets_mapping else {})}


class TokenizationAuditTests(unittest.TestCase):
    def test_boundary_special_tokens_and_denominator(self):
        report = summarize_counts([510, 512, 514], max_sequence_length=512)
        self.assertEqual(report["chunks_exceeding_window"], 1)
        self.assertEqual(report["tokens_beyond_window"], 2)
        self.assertAlmostEqual(report["tokens_beyond_window_fraction"], 2 / 1536)
        self.assertEqual(report["p50_nearest_rank"], 512)
        self.assertEqual(report["p95_nearest_rank"], 514)

    def test_inherited_and_new_rows_are_measured_separately(self):
        rows = [{"news_id": "old", "text": "a" * 100},
                {"news_id": "new", "text": "b" * 510},
                {"news_id": "new", "text": "c" * 800}]
        report = audit_rows(rows, tokenizer=FakeTokenizer(), base_chunks=1,
                            max_sequence_length=512, batch_size=2)
        self.assertEqual(report["inherited_base"]["chunks_exceeding_window"], 0)
        self.assertEqual(report["new_in_this_package"]["articles"], 1)
        self.assertEqual(report["new_in_this_package"]["chunks_exceeding_window"], 1)
        self.assertEqual(report["new_in_this_package"]["tokens_beyond_window"], 290)
        self.assertEqual(report["new_in_this_package"]["row_range_half_open"], [1, 3])

    def test_actual_truncated_encoding_is_checked(self):
        class BrokenTokenizer(FakeTokenizer):
            def __call__(self, texts, *, truncation, **kwargs):
                return super().__call__(texts, truncation=False, **kwargs)

        with self.assertRaisesRegex(ValueError, "expected single-sequence limit"):
            audit_rows([{"news_id": "a", "text": "a" * 800}],
                       tokenizer=BrokenTokenizer(), base_chunks=0, max_sequence_length=512)

    def test_left_truncation_and_invalid_scope_are_rejected(self):
        tokenizer = FakeTokenizer()
        tokenizer.truncation_side = "left"
        with self.assertRaisesRegex(ValueError, "right-side"):
            audit_rows([], tokenizer=tokenizer, base_chunks=0, max_sequence_length=512)
        with self.assertRaisesRegex(ValueError, "base boundary"):
            audit_rows([], tokenizer=FakeTokenizer(), base_chunks=1, max_sequence_length=512)

    def test_empty_scope_does_not_invent_zero_percentages(self):
        report = summarize_counts([], max_sequence_length=512)
        self.assertIsNone(report["tokens_beyond_window_fraction"])
        self.assertIsNone(report["chunks_exceeding_window_fraction"])
        self.assertIsNone(report["p95_nearest_rank"])

    def coverage_fixture(self, root):
        article = {"news_id": "new", "title": "title", "content": " abcdefghijk \n",
                   "url": "https://news.qq.com/rain/a/new", "content_version": 1,
                   "publish_time": "2026-10-14T08:00:00+08:00"}
        raw = json.dumps(article).encode()
        (root / "articles").mkdir()
        (root / "articles/new.json").write_bytes(raw)
        digest = sha256(f"{article['title']}\x1f{article['content']}".encode()).hexdigest()
        header = f"# title\n来源地址: {article['url']}\n发布时间: {article['publish_time']}\n\n"
        rows = [{"news_id": "new", "chunk_index": index,
                 "text": header + article["content"].strip()[start:start + 6],
                 "document_sha256": digest, "content_version": 1}
                for index, start in enumerate((0, 4, 8))]
        build = {"input": {"manifest_path": str(root / "manifest.json"), "articles": [{
            "news_id": "new", "relative_path": "articles/new.json", "sha256": sha256(raw).hexdigest(),
            "document_sha256": digest}]}, "chunking": {"body_chars": 6, "overlap_chars": 2}}
        return build, rows, len(header)

    def test_overlap_restores_body_coverage_despite_per_chunk_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            build, rows, header_chars = self.coverage_fixture(Path(temporary))
            report = audit_body_coverage(rows, build=build, tokenizer=FakeTokenizer(),
                                         max_sequence_length=header_chars + 2 + 4, batch_size=2)
        self.assertEqual(report["canonical_body_characters"], 11)
        self.assertEqual(report["outer_whitespace_characters_removed_by_split_text_strip"], 3)
        self.assertEqual(report["uncovered_body_characters"], 0)
        self.assertEqual(report["body_character_coverage_fraction"], 1.0)

    def test_missing_body_intervals_are_unioned_in_original_coordinates(self):
        with tempfile.TemporaryDirectory() as temporary:
            build, rows, header_chars = self.coverage_fixture(Path(temporary))
            report = audit_body_coverage(rows, build=build, tokenizer=FakeTokenizer(),
                                         max_sequence_length=header_chars + 2 + 3, batch_size=2)
            rows[0]["text"] += "tampered"
            with self.assertRaisesRegex(ValueError, "exactly match"):
                audit_body_coverage(rows, build=build, tokenizer=FakeTokenizer(),
                                    max_sequence_length=512, batch_size=2)
        self.assertEqual(report["uncovered_body_characters"], 2)
        self.assertEqual(report["affected_articles"][0]["gaps_half_open"], [(3, 4), (7, 8)])
        self.assertEqual(report["individual_gap_characters"]["max"], 1)

    def test_coverage_union_handles_nested_adjacent_and_empty_spans(self):
        self.assertEqual(uncovered_intervals(12, [(1, 5), (2, 3), (5, 8), (10, 10)]),
                         [(0, 1), (8, 12)])


if __name__ == "__main__":
    unittest.main()
