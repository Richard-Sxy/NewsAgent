"""Offline tests for review-source integrity, HTML safety and human-only labels."""
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from tools.prepare_annual_news_review import prepare, public_link, verify_records


def digest(raw):
    return sha256(raw).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fixture(root, *, publication="2026-02-03T12:13:14+08:00", precision="second",
            malicious=False, second_url=False, http=False):
    corpus = root / "corpus"
    url = ("http" if http else "https") + "://www.news.cn/20260203/example/c.html"
    urls = [url]
    if second_url:
        urls.append(url.replace("example", "different"))
    entries, paths = [], []
    body = "离线构造的新闻正文，保留各来源和原文证据，供人工核查与来源校验。" * 10
    title = "测试新闻标题"
    if malicious:
        body += '</pre><script>alert("body")</script><img src=x onerror="alert(1)">'
        title = '</h4><script>alert("title")</script>'
    for index, source_url in enumerate(urls):
        identity = digest(source_url.encode("utf-8"))[:24]
        raw = ("<html>" + body + "</html>").encode("utf-8")
        raw_relative = "raw/" + identity + ".html"
        (corpus / "raw").mkdir(parents=True, exist_ok=True)
        (corpus / raw_relative).write_bytes(raw)
        article = {
            "schema_version": 1, "parser_version": 1, "article_id": identity,
            "url": source_url, "title": title, "body": body, "status": "body_ready",
            "published_at": publication, "publication_date": publication[:10],
            "date_precision": precision, "source_timezone": "+08:00" if "+08:00" in publication else None,
            "date_basis": "meta:publishdate", "date_evidence": publication,
            "fetched_at": "2026-10-27T01:00:00+00:00", "body_basis": "dom:article",
            "event_ids": ["event-one", "topic-one"], "categories": ["tech", "AI"],
            "discovery_hints": [{"event_id": "event-one", "role_hint": "follow_up", "verified": False}],
            "quality_flags": [], "body_sha256": digest(body.encode("utf-8")),
            "raw_path": raw_relative, "raw_sha256": digest(raw),
            "relevance_label": "automatic_bad_label",
        }
        relative = "articles/" + identity + ".json"
        write_json(corpus / relative, article)
        paths.append(corpus / relative)
        entries.append({
            "article_id": identity, "url": source_url, "status": "body_ready",
            "relative_path": relative, "parsed_sha256": digest((corpus / relative).read_bytes()),
            "raw_sha256": article["raw_sha256"], "body_sha256": article["body_sha256"],
            **{key: article[key] for key in ("published_at", "date_precision", "date_basis", "event_ids", "categories")},
        })
    entries.append({"url": "https://www.news.cn/missing/c.html", "article_id": "missing",
                    "status": "failed", "error": "HTTP_404", "event_ids": ["event-empty"],
                    "categories": ["科技"]})
    write_json(corpus / "report.json", {
        "schema_version": 1, "unique_candidates": len(entries) + 2, "articles": entries,
        "scope": {"start_date": "2025-10-27", "end_date": "2026-10-27"},
    })
    discovery = root / "discovery.json"
    write_json(discovery, {"events": [
        {"event_id": "event-one", "title": "真实事件发现组", "category": "科技",
         "event_date": "2026-02-03", "articles": [{"url": item} for item in urls]},
        {"event_id": "topic-one", "title": "科技年度专题", "category": "科技",
         "discovery_group_type": "topic_collection", "event_date": None,
         "articles": [{"url": item} for item in urls]},
        {"event_id": "event-empty", "title": "尚无正文事件", "category": "体育",
         "event_date": "2026-02-04", "articles": [{"url": "https://www.news.cn/missing/c.html"}]},
    ]})
    return corpus, discovery, paths


def resign_parsed(corpus, article_path):
    report_path = corpus / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for row in report["articles"]:
        if row.get("relative_path") == article_path.relative_to(corpus).as_posix():
            row["parsed_sha256"] = digest(article_path.read_bytes())
    write_json(report_path, report)


class AnnualNewsReviewTests(unittest.TestCase):
    def test_verified_export_keeps_all_human_labels_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, discovery, _ = fixture(root)
            summary = prepare(corpus, [discovery], root / "out", repository_root=root)
            records = [json.loads(line) for line in (root / "out/review-records.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            row = records[0]
            self.assertIsNone(row["relevance_label"])
            self.assertEqual(row["relevance_review_status"], "pending_human")
            self.assertTrue(all(value is None for value in row["human_labels"].values()))
            self.assertIsNone(row["fact_group_id"])
            self.assertEqual(row["relation_edges"], [])
            self.assertEqual(row["event_membership_status"], "pending_human")
            self.assertIsNone(summary["retrieval_accuracy"])
            self.assertEqual(len(list((root / "out").iterdir())), 4)

    def test_exact_duplicates_retain_all_sources_and_only_suggest_representative(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, discovery, _ = fixture(root, second_url=True)
            summary = prepare(corpus, [discovery], root / "out", repository_root=root)
            self.assertEqual(summary["body_ready"], 2)
            self.assertEqual(summary["exact_duplicate_group_count"], 1)
            group = summary["exact_duplicate_groups"][0]
            self.assertEqual(len(group["sources"]), 2)
            self.assertEqual(group["fact_equivalence_status"], "pending_human")
            self.assertEqual(group["representative_status"], "suggestion_only")
            self.assertEqual(summary["event_coverage"][0]["body_ready"], 0)

    def test_discovery_topic_collections_are_not_counted_as_real_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, discovery, _ = fixture(root)
            summary = prepare(corpus, [discovery, discovery], root / "out", repository_root=root)
            self.assertEqual(summary["discovery_real_event_count"], 2)
            self.assertEqual(summary["discovery_topic_collection_count"], 1)
            self.assertEqual(summary["events_without_verified_body"], 1)
            self.assertEqual(summary["discovery_unique_url_count"], 2)
            self.assertEqual(summary["category_coverage"]["科技"], 1)
            self.assertEqual(summary["category_coverage"]["AI"], 1)
            self.assertEqual(summary["month_coverage"], {"2026-02": 1})

    def test_tampered_parsed_file_rejected_before_any_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, discovery, paths = fixture(root)
            paths[0].write_text(paths[0].read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "parsed article SHA"):
                prepare(corpus, [discovery], root / "out", repository_root=root)
            self.assertFalse((root / "out").exists())

    def test_tampered_body_even_with_resigned_parsed_hash_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, _, paths = fixture(root)
            article = json.loads(paths[0].read_text(encoding="utf-8"))
            article["body"] += "tampered"
            write_json(paths[0], article)
            resign_parsed(corpus, paths[0])
            with self.assertRaisesRegex(ValueError, "body SHA"):
                verify_records(corpus)

    def test_tampered_raw_html_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, _, paths = fixture(root)
            article = json.loads(paths[0].read_text(encoding="utf-8"))
            (corpus / article["raw_path"]).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "raw HTML SHA"):
                verify_records(corpus)

    def test_report_and_parsed_metadata_must_agree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, _, paths = fixture(root)
            article = json.loads(paths[0].read_text(encoding="utf-8"))
            article["published_at"] = "2026-05-03T12:13:14+08:00"
            write_json(paths[0], article)
            resign_parsed(corpus, paths[0])
            with self.assertRaisesRegex(ValueError, "metadata differs"):
                verify_records(corpus)

    def test_html_escapes_all_external_title_and_body_markup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, discovery, _ = fixture(root, malicious=True)
            prepare(corpus, [discovery], root / "out", repository_root=root)
            html = (root / "out/index.html").read_text(encoding="utf-8")
            self.assertNotIn('<script>alert("title")', html)
            self.assertNotIn('<script>alert("body")', html)
            self.assertNotIn('<img src=x', html)
            self.assertIn("&lt;script&gt;", html)
            self.assertNotIn("innerHTML", html)
            self.assertIn("card.textContent", html)

    def test_exact_timezone_publication_is_preserved_in_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, discovery, _ = fixture(root)
            prepare(corpus, [discovery], root / "out", repository_root=root)
            row = json.loads((root / "out/content-candidates.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(row["publish_time"], "2026-02-03T12:13:14+08:00")
            self.assertTrue(row["news_id"].startswith("pub-"))
            self.assertEqual(row["content_state"], "body_available")
            self.assertEqual(row["source"]["path"][:7], "corpus/")
            self.assertEqual(len(row["source"]["file_sha256"]), 64)
            self.assertEqual(len(row["source"]["manifest_sha256"]), 64)
            self.assertIsNone(row["relevance_label"])

    def test_day_only_publication_has_no_invented_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, discovery, _ = fixture(root, publication="2026-02-03", precision="day")
            summary = prepare(corpus, [discovery], root / "out", repository_root=root)
            row = json.loads((root / "out/content-candidates.jsonl").read_text(encoding="utf-8"))
            self.assertNotIn("publish_time", row)
            self.assertEqual(row["date_precision"], "day")
            self.assertEqual(row["publication_date"], "2026-02-03")
            self.assertEqual(row["source"]["published_at_original"], "2026-02-03")
            self.assertEqual(summary["content_candidates_publication_pending"], 1)

    def test_naive_datetime_has_no_invented_timezone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, discovery, _ = fixture(root, publication="2026-02-03T12:13:14")
            prepare(corpus, [discovery], root / "out", repository_root=root)
            row = json.loads((root / "out/content-candidates.jsonl").read_text(encoding="utf-8"))
            self.assertNotIn("publish_time", row)
            self.assertEqual(row["source"]["published_at_original"], "2026-02-03T12:13:14")

    def test_http_can_be_reviewed_but_is_not_a_content_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, discovery, _ = fixture(root, http=True)
            summary = prepare(corpus, [discovery], root / "out", repository_root=root)
            self.assertEqual(summary["body_ready"], 1)
            self.assertEqual(summary["content_candidate_count"], 0)
            self.assertEqual(summary["content_candidate_skipped"]["http_source"], 1)

    def test_outside_repository_evidence_does_not_produce_invalid_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, discovery, _ = fixture(root)
            summary = prepare(corpus, [discovery], root / "out", repository_root=root / "other")
            self.assertEqual(summary["content_candidate_count"], 0)
            self.assertEqual(summary["content_candidate_skipped"]["outside_repository"], 1)

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, _, _ = fixture(root)
            path = corpus / "report.json"
            report = json.loads(path.read_text(encoding="utf-8"))
            report["articles"][0]["relative_path"] = "../bad.json"
            write_json(path, report)
            with self.assertRaises(ValueError):
                verify_records(corpus)

    def test_unsafe_links_are_not_renderable(self):
        for link in ("javascript:alert(1)", "https://u:p@news.cn/a", "file:///etc/passwd"):
            self.assertIsNone(public_link(link))
        self.assertEqual(public_link("https://news.cn/a?token=secret"), "https://news.cn/a")

    def test_collector_report_repeated_urls_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus, _, _ = fixture(root)
            path = corpus / "report.json"
            report = json.loads(path.read_text(encoding="utf-8"))
            report["articles"].append(report["articles"][0])
            write_json(path, report)
            with self.assertRaisesRegex(ValueError, "repeats"):
                verify_records(corpus)


if __name__ == "__main__":
    unittest.main()

