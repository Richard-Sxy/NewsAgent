"""Dataset integrity tests; no network, model weights or third-party packages."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from experiments.recall_dataset import (
    load_articles_chunks,
    load_questions,
    load_snapshot_chunks,
    news_id_from_url,
)


class RecallDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def write_json(self, name, value):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def excerpt(self, body="真实报道切片", news_id="20260820A01ABC00", title="新闻标题"):
        return {
            "text": f"# {title}\n\n来源地址: https://news.qq.com/rain/a/{news_id}\n"
                    f"作者: 腾讯网\n发布时间: 2026-08-20 12:00:00\n\n{body}",
            "source_name": "fallback.txt",
        }

    def snapshot(self, records, **extra):
        return {"items": [{"retrieved": records, **extra}]}

    def question(self, identity="q1", news_id="20260820A01ABC00"):
        return {
            "id": identity,
            "question": "相关事件是什么？",
            "expected_urls": [f"https://news.qq.com/rain/a/{news_id}"],
            "reference_answer": "不得成为索引正文的答案",
            "topic": "科技",
        }

    def freeze_articles(self, articles):
        """Write collector fixtures in the manifest's specified article order."""
        ids = [article["news_id"] for article in articles]
        for article in articles:
            self.write_json(f"articles/{article['news_id']}.json", article)
        canonical = json.dumps(
            articles, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        manifest = {
            "successful_news_ids": ids,
            "hash": hashlib.sha256(canonical).hexdigest(),
            "source_kind": "public_news_articles",
        }
        self.write_json("articles/collection_manifest.json", manifest)
        return self.directory / "articles", manifest

    def test_news_id_requires_trusted_url_identity(self):
        self.assertEqual(news_id_from_url("https://news.qq.com/rain/a/N123?x=1#part"), "N123")
        for url in ["http://news.qq.com/rain/a/N123", "https://news.qq.com.evil.test/rain/a/N123",
                    "https://user@news.qq.com/rain/a/N123", "https://news.qq.com/rain/a/",
                    "tencent-news-N123.txt", None]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                news_id_from_url(url)

    def test_snapshot_uses_retrieved_text_and_removes_only_preamble(self):
        path = self.write_json("snapshot.json", self.snapshot(
            [self.excerpt(body="报道正文\n作者: 正文中的引用")],
            question="不应索引的问题", reference_answer="不应索引的答案"))
        chunks, provenance = load_snapshot_chunks([path])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["title"], "新闻标题")
        self.assertEqual(chunks[0]["text"], "新闻标题\n报道正文\n作者: 正文中的引用")
        self.assertEqual(chunks[0]["source_snapshot"], str(path.resolve()))
        self.assertEqual(len(chunks[0]["source_text_hash"]), 64)
        self.assertEqual(provenance["source_kind"], "historical_retrieval_excerpts")
        self.assertIn("selection bias", " ".join(provenance["limitations"]))

    def test_snapshot_deduplicates_body_per_news_id_not_title(self):
        first = self.write_json("first.json", self.snapshot([self.excerpt()]))
        second = self.write_json("second.json", self.snapshot([
            self.excerpt(title="同一正文的新标题"), self.excerpt(news_id="OTHER"),
            self.excerpt(body="另一切片")]))
        chunks, provenance = load_snapshot_chunks([first, second])
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len({chunk["chunk_id"] for chunk in chunks}), 3)
        self.assertEqual(provenance["excluded_records"]["duplicate_news_id_and_body"], 1)

    def test_snapshot_fallback_title_and_exclusion_counts(self):
        fallback = {"source_name": "fallback.txt", "text": "来源地址: https://news.qq.com/rain/a/A\n正文"}
        path = self.write_json("snap.json", self.snapshot([
            fallback, {"text": "", "source_name": ""},
            {"text": "只有正文", "source_name": "tencent-news-A.txt"},
            self.excerpt(body="")]))
        chunks, provenance = load_snapshot_chunks([path])
        self.assertEqual(chunks[0]["title"], "fallback.txt")
        self.assertEqual(provenance["excluded_records"], {
            "empty_text": 1, "missing_or_invalid_source_url": 1, "empty_body": 1})

    def test_snapshot_conflicting_source_identity_fails(self):
        record = self.excerpt()
        record["text"] = record["text"].replace("作者:", "来源地址: https://news.qq.com/rain/a/OTHER\n作者:")
        path = self.write_json("snap.json", self.snapshot([record]))
        with self.assertRaisesRegex(ValueError, "conflicting source"):
            load_snapshot_chunks([path])

    def test_snapshot_keeps_body_heading_and_metadata_versions(self):
        record = self.excerpt(body="# 正文中的标题\n报道正文")
        record["news_id"] = "20260820A01ABC00"
        record["content_version"] = "v2"
        path = self.write_json("snap.json", self.snapshot([record]))
        chunks, provenance = load_snapshot_chunks([path])
        self.assertIn("# 正文中的标题", chunks[0]["text"])
        self.assertEqual(chunks[0]["content_version"], "v2")
        self.assertEqual(chunks[0]["publish_time"], "2026-08-20 12:00:00")
        self.assertFalse(provenance["rechunked"])
        record["news_id"] = "CONFLICT"
        path = self.write_json("snap.json", self.snapshot([record]))
        with self.assertRaisesRegex(ValueError, "news_id conflicts"):
            load_snapshot_chunks([path])

    def test_articles_repeat_title_within_total_character_cap(self):
        self.write_json("articles/a.json", {"url": "https://news.qq.com/rain/a/A", "title": "标题", "content": "abcdefghijklmnop", "publish_time": "2026-08-20", "content_version": "v3"})
        chunks, provenance = load_articles_chunks(self.directory / "articles", chunk_chars=10, overlap=2)
        self.assertEqual([chunk["text"] for chunk in chunks], ["标题\nabcdefg", "标题\nfghijkl", "标题\nklmnop"])
        self.assertTrue(all(len(chunk["text"]) <= 10 for chunk in chunks))
        self.assertEqual(provenance["chunk_count"], 3)
        self.assertEqual(chunks[0]["content_version"], "v3")
        self.assertEqual(chunks[0]["publish_time"], "2026-08-20")

    def test_articles_reject_conflicting_ids_and_versions(self):
        self.write_json("articles/a.json", {"url": "https://news.qq.com/rain/a/A", "news_id": "B", "content": "正文"})
        with self.assertRaisesRegex(ValueError, "news_id conflicts"):
            load_articles_chunks(self.directory / "articles")
        self.write_json("articles/a.json", {"url": "https://news.qq.com/rain/a/A", "content": "正文"})
        self.write_json("articles/b.json", {"url": "https://news.qq.com/rain/a/A", "content": "更新后的正文"})
        with self.assertRaisesRegex(ValueError, "conflicting article versions"):
            load_articles_chunks(self.directory / "articles")

    def test_articles_empty_body_and_duplicate_counts(self):
        article = {"url": "https://news.qq.com/rain/a/A", "content": "正文"}
        self.write_json("articles/a.json", article)
        self.write_json("articles/b.json", article)
        self.write_json("articles/c.json", {"content": " "})
        chunks, provenance = load_articles_chunks(self.directory / "articles")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(provenance["excluded_records"], {"duplicate_article": 1, "empty_body": 1})

    def test_articles_reject_different_versions_even_with_same_body(self):
        article = {"url": "https://news.qq.com/rain/a/A", "content": "正文", "content_version": "v1"}
        self.write_json("articles/a.json", article)
        article["content_version"] = "v2"
        self.write_json("articles/b.json", article)
        with self.assertRaisesRegex(ValueError, "conflicting article versions"):
            load_articles_chunks(self.directory / "articles")

    def test_articles_reject_too_long_title_instead_of_truncating(self):
        self.write_json("articles/a.json", {"url": "https://news.qq.com/rain/a/A", "title": "标题占满所有字符", "content": "正文"})
        with self.assertRaisesRegex(ValueError, "insufficient body capacity"):
            load_articles_chunks(self.directory / "articles", chunk_chars=10, overlap=2)

    def test_frozen_manifest_accepts_canonical_json_and_preserves_selection_order(self):
        articles = [
            {"news_id": "B", "url": "https://news.qq.com/rain/a/B", "title": "乙", "content": "乙新闻正文"},
            {"news_id": "A", "url": "https://news.qq.com/rain/a/A", "title": "甲", "content": "甲新闻正文"},
        ]
        directory, manifest = self.freeze_articles(articles)
        # JSON key ordering and whitespace are not part of canonical identity.
        (directory / "B.json").write_text(
            json.dumps(dict(reversed(list(articles[0].items()))), ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        chunks, provenance = load_articles_chunks(directory)
        self.assertEqual([chunk["news_id"] for chunk in chunks], ["B", "A"])
        self.assertEqual(provenance["collection_manifest"], manifest)
        self.assertEqual(provenance["article_files"], 2)

    def test_frozen_manifest_ignores_unselected_articles_and_miscellaneous_json(self):
        directory, _ = self.freeze_articles([
            {"news_id": "A", "url": "https://news.qq.com/rain/a/A", "content": "纳入本次语料的正文"},
        ])
        self.write_json("articles/OTHER.json", {
            "url": "https://news.qq.com/rain/a/OTHER", "content": "另一次采集的正文",
        })
        self.write_json("articles/diagnostics.json", {"errors": ["临时采集日志"]})
        (directory / "broken.json").write_text("not JSON", encoding="utf-8")
        chunks, provenance = load_articles_chunks(directory)
        self.assertEqual([chunk["news_id"] for chunk in chunks], ["A"])
        self.assertEqual(provenance["article_files"], 1)
        self.assertEqual(provenance["news_count"], 1)

    def test_frozen_manifest_rejects_article_body_changes(self):
        directory, _ = self.freeze_articles([
            {"news_id": "A", "url": "https://news.qq.com/rain/a/A", "content": "冻结时的正文"},
        ])
        self.write_json("articles/A.json", {
            "news_id": "A", "url": "https://news.qq.com/rain/a/A", "content": "冻结后的修改正文",
        })
        with self.assertRaisesRegex(ValueError, "frozen article corpus hash differs"):
            load_articles_chunks(directory)

    def test_frozen_manifest_rejects_missing_selected_article(self):
        directory, _ = self.freeze_articles([
            {"news_id": "A", "url": "https://news.qq.com/rain/a/A", "content": "完整正文"},
        ])
        (directory / "A.json").unlink()
        with self.assertRaisesRegex(ValueError, "cannot read JSON dataset.*A.json"):
            load_articles_chunks(directory)

    def test_frozen_manifest_requires_object_and_valid_identity(self):
        directory = self.directory / "articles"
        self.write_json("articles/collection_manifest.json", [])
        with self.assertRaisesRegex(ValueError, "manifest must be an object"):
            load_articles_chunks(directory)
        article = {"url": "https://news.qq.com/rain/a/A", "content": "完整正文"}
        self.write_json("articles/B.json", article)
        digest = hashlib.sha256(json.dumps(
            [article], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        self.write_json("articles/collection_manifest.json", {
            "successful_news_ids": ["B"], "hash": digest,
        })
        with self.assertRaisesRegex(ValueError, "news ID differs from article source URL"):
            load_articles_chunks(directory)

    def test_queries_namespace_reused_ids_and_exclude_answers(self):
        first = self.write_json("retrieval_questions.json", [self.question()])
        second = self.write_json("retrieval_questions_semantic.json", [self.question()])
        rows = load_questions([first, second])
        self.assertEqual([row["query_id"] for row in rows], ["retrieval_questions:q1", "retrieval_questions_semantic:q1"])
        self.assertEqual(rows[0]["relevance"], {"20260820A01ABC00": 1})
        self.assertEqual(set(rows[0]), {"query_id", "question", "relevance", "topic"})

    def test_queries_reject_row_id_and_namespace_collisions(self):
        first = self.write_json("first/q.json", [self.question(), self.question()])
        with self.assertRaisesRegex(ValueError, "duplicate query ID"):
            load_questions([first])
        first = self.write_json("first/q.json", [self.question()])
        second = self.write_json("second/q.json", [self.question("q2")])
        with self.assertRaisesRegex(ValueError, "duplicate question dataset namespace"):
            load_questions([first, second])

    def test_queries_reject_missing_or_invalid_labels(self):
        row = self.question()
        row["expected_urls"] = ["https://example.org/A"]
        path = self.write_json("q.json", [row])
        with self.assertRaisesRegex(ValueError, "expected https"):
            load_questions([path])
        row["expected_urls"] = []
        path = self.write_json("q.json", [row])
        with self.assertRaisesRegex(ValueError, "requires expected_urls"):
            load_questions([path])


if __name__ == "__main__":
    unittest.main()
