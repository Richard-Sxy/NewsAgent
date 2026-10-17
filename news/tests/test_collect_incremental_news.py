"""Offline security, date-window, success quotas and continuation checks."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request

from experiments.collect_incremental_news import (
    FEED_URL, MAX_FEED_BYTES, RejectFeedRedirect, collect, discover,
    extract_candidates, feed_request, fetch_feed, json_bytes, publication_time,
    sha256, validate_article, verify_frozen_articles, write_json, load_external_discovery,
    seed_from_manifest, recover_pending_articles, RequestLimiter, collection_contract, main,
)
from experiments.collect_recall_articles import parse_article

START, END = date(2026, 10, 7), date(2026, 10, 14)
URL = "https://news.qq.com/rain/a/20261014A00001"
HTML = ('<h1>真实新闻</h1><meta property="article:published_time" '
        'content="2026-10-14 12:30:00"><article><p>第一段完整原文。</p>'
        '<p>第二段正文。</p></article>')


def candidate(suffix="00001", published="2026-10-14T12:30:00+08:00"):
    return {"news_id": "20261014A" + suffix, "url": "https://news.qq.com/rain/a/20261014A" + suffix,
            "category": "tech", "channels": ["tech"], "feed_publish_time": published, "feed_title": "候选"}


def source(root, rows):
    discovery = {"candidates": rows, "settings": {"pages_per_channel": 1, "page_size": 50}}
    write_json(root / "discovery_manifest.json", discovery)
    return discovery


class CollectIncrementalNewsTests(unittest.TestCase):
    def test_publication_timezone_is_beijing_and_missing_is_rejected(self):
        self.assertEqual(publication_time("2026-10-13T17:00:00Z").isoformat(), "2026-10-14T01:00:00+08:00")
        self.assertEqual(publication_time("2026-10-07 01:02:03").isoformat(), "2026-10-07T01:02:03+08:00")
        for invalid in ("", None, "not a timestamp"):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, TypeError)):
                publication_time(invalid)

    def test_feed_request_is_fixed_and_validated(self):
        first = feed_request("tech", 0, 50)
        self.assertEqual(first["forward"], "2")
        self.assertEqual(feed_request("tech", 1, 50)["forward"], "1")
        self.assertEqual(feed_request("tech", 999, 50)["flush_num"], 999)
        for channel, page, size in (("evil", 0, 50), ("tech", 1000, 50), ("tech", 1, 51), ("tech", True, 50)):
            with self.assertRaises(ValueError):
                feed_request(channel, page, size)

    def test_discovery_filters_video_aggregation_missing_time_and_outside_window(self):
        rows = [{"id": "20261014A00001", "articletype": "0", "publish_time": "2026-10-14 12:30:00"},
                {"id": "20261014V00002", "articletype": "4", "publish_time": "2026-10-14 12:30:00"},
                {"id": "UTR20261014", "articletype": "116", "publish_time": "2026-10-14 12:30:00"},
                {"id": "20261006A00003", "articletype": "0", "publish_time": "2026-10-06 12:30:00"},
                {"id": "20261014A00004", "articletype": "0", "publish_time": ""},
                {"id": "../../escape", "articletype": "0", "publish_time": "2026-10-14 12:30:00"}]
        found, rejected = extract_candidates({"code": 0, "data": [{"sub_item": rows}]}, "tech", START, END)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["url"], URL)
        self.assertEqual(len(rejected), 5)

    def test_feed_redirect_and_final_url_are_rejected_before_read(self):
        with self.assertRaises(ValueError):
            RejectFeedRedirect().redirect_request(Request(FEED_URL), None, 302, "Found", {}, "https://evil.test/feed")
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://evil.test/feed"
        with patch("experiments.collect_incremental_news.build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(ValueError):
                fetch_feed("tech", 0, 50, 1)
            response.read.assert_not_called()

    def test_feed_read_is_bounded(self):
        for raw in (b"", b"x" * (MAX_FEED_BYTES + 1)):
            response = MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = FEED_URL
            response.read.return_value = raw
            with patch("experiments.collect_incremental_news.build_opener") as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(ValueError):
                    fetch_feed("tech", 0, 50, 1)
                response.read.assert_called_once_with(MAX_FEED_BYTES + 1)

    def test_discovery_cache_and_response_hash_preserve_original_bytes(self):
        payload = {"code": 0, "data": [{"id": "20261014A00001", "articletype": "0",
                                        "publish_time": "2026-10-14 12:30:00"}]}
        raw = json_bytes(payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.CHANNELS", ("tech",)), patch(
                    "experiments.collect_incremental_news.fetch_feed", return_value=raw) as fetch:
                first = discover(root, START, END, pages_per_channel=2, interval=0)
                second = discover(root, START, END, pages_per_channel=2, interval=0)
                self.assertEqual(fetch.call_count, 2)
            self.assertEqual(len(first["candidates"]), 1)
            self.assertEqual(len(first["duplicates"]), 1)
            self.assertEqual(first["feed_requests"][0]["response_sha256"], sha256(raw))
            self.assertTrue(all(row["cached"] for row in second["feed_requests"]))

    def test_cache_cannot_be_misattributed_to_a_different_page_size(self):
        payload = {"code": 0, "data": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.CHANNELS", ("tech",)), patch(
                    "experiments.collect_incremental_news.fetch_feed", return_value=json_bytes(payload)) as fetch:
                discover(root, START, END, pages_per_channel=1, page_size=50, interval=0)
                with self.assertRaisesRegex(ValueError, "request does not match"):
                    discover(root, START, END, pages_per_channel=1, page_size=10, interval=0)
                fetch.assert_called_once()

    def test_tampered_feed_response_blocks_entire_continuation_without_rewriting_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.CHANNELS", ("tech",)), patch(
                    "experiments.collect_incremental_news.fetch_feed", return_value=json_bytes({"code": 0, "data": []})):
                discover(root, START, END, pages_per_channel=1, interval=0)
            before = (root / "discovery_manifest.json").read_bytes()
            (root / "discovery/tech/page-000.json").write_bytes(b'{"code":0,"data":[],"tampered":true}')
            with patch("experiments.collect_incremental_news.CHANNELS", ("tech",)), patch(
                    "experiments.collect_incremental_news.fetch_feed") as fetch:
                with self.assertRaisesRegex(ValueError, "feed response SHA-256"):
                    discover(root, START, END, pages_per_channel=1, interval=0)
                fetch.assert_not_called()
            self.assertEqual((root / "discovery_manifest.json").read_bytes(), before)

    def test_tampered_feed_request_sidecar_blocks_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.CHANNELS", ("tech",)), patch(
                    "experiments.collect_incremental_news.fetch_feed", return_value=json_bytes({"code": 0, "data": []})):
                discover(root, START, END, pages_per_channel=1, interval=0)
            request = feed_request("tech", 0, 50)
            request["item_count"] = 10
            write_json(root / "discovery/tech/page-000.request.json", request)
            with patch("experiments.collect_incremental_news.CHANNELS", ("tech",)), patch(
                    "experiments.collect_incremental_news.fetch_feed") as fetch:
                with self.assertRaisesRegex(ValueError, "request parameters differ"):
                    discover(root, START, END, pages_per_channel=1, interval=0)
                fetch.assert_not_called()

    def test_article_metadata_date_is_authoritative_and_never_filled_from_feed(self):
        invalid = [HTML.replace('content="2026-10-14 12:30:00"', 'content=""'),
                   HTML.replace("2026-10-14 12:30:00", "2026-10-06 12:30:00"),
                   "<h1>有标题无正文</h1>"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [candidate(f"{i:05}") for i in range(3)]
            with patch("experiments.collect_incremental_news.fetch_html", side_effect=invalid):
                manifest = collect(root, source(root, rows), START, END, workers=1)
            self.assertEqual(manifest["success"], 0)
            self.assertEqual(len(manifest["failures"]), 2)
            self.assertEqual(len(manifest["out_of_window"]), 1)
            self.assertEqual(manifest["missing_dates"], ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10",
                                                        "2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14"])
            self.assertFalse(list((root / "articles").glob("*.json")))

    def test_success_quota_counts_actual_bodies_and_deduplicates_content(self):
        rows = [candidate(f"{i:05}") for i in range(5)]
        good2 = HTML.replace("真实新闻", "第二篇")
        good3 = HTML.replace("真实新闻", "第三篇")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.fetch_html", side_effect=["", HTML, HTML, good2, good3]):
                manifest = collect(root, source(root, rows), START, END, workers=1, per_channel_day=2)
            self.assertEqual(manifest["success"], 2)
            self.assertEqual(manifest["counts_by_date"]["2026-10-14"], 2)
            self.assertEqual(len(manifest["failures"]), 1)
            self.assertEqual(len(manifest["duplicate_content"]), 1)
            self.assertEqual(len(manifest["skipped"]), 1)
            # The last candidate is fetched: a feed date cannot establish its
            # real article date or justify a quota skip before HTML validation.
            self.assertEqual(manifest["attempted_articles"], 5)

    def test_article_date_controls_statistics_when_feed_date_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [candidate(published="2026-10-13T12:30:00+08:00")]
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML):
                manifest = collect(root, source(root, rows), START, END, workers=1)
            self.assertEqual(manifest["counts_by_date"]["2026-10-13"], 0)
            self.assertEqual(manifest["counts_by_date"]["2026-10-14"], 1)

    def test_frozen_article_cache_reused_and_file_hash_manifest_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = source(root, [candidate()])
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML) as fetch:
                first = collect(root, discovery, START, END, workers=1)
                second = collect(root, discovery, START, END, workers=1)
                fetch.assert_called_once()
            row = first["articles"][0]
            raw = (root / row["relative_path"]).read_bytes()
            article = json.loads(raw)
            self.assertEqual(row["sha256"], sha256(raw))
            self.assertEqual(row["document_sha256"], sha256((article["title"] + "\x1f" + article["content"]).encode()))
            self.assertEqual(article["publish_time"], "2026-10-14T12:30:00+08:00")
            self.assertEqual(first["corpus_sha256"], second["corpus_sha256"])
            self.assertEqual(second["cached"], 1)

    def test_existing_corpus_survives_changed_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML):
                collect(root, source(root, [candidate()]), START, END, workers=1)
            frozen = (root / "manifest.json").read_bytes()
            with patch("experiments.collect_incremental_news.fetch_html") as fetch:
                with self.assertRaisesRegex(ValueError, "collection contract changed"):
                    collect(root, source(root, []), START, END, workers=1)
                fetch.assert_not_called()
            self.assertEqual((root / "manifest.json").read_bytes(), frozen)
            self.assertEqual(json.loads(frozen)["success"], 1)

    def test_tampered_article_blocks_entire_continuation_without_resigning_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = source(root, [candidate()])
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML):
                manifest = collect(root, discovery, START, END, workers=1)
            before = (root / "manifest.json").read_bytes()
            path = root / manifest["articles"][0]["relative_path"]
            article = json.loads(path.read_bytes())
            article["content"] = "被改动的正文仍然非空。"
            write_json(path, article)
            with patch("experiments.collect_incremental_news.fetch_html") as fetch:
                with self.assertRaisesRegex(ValueError, "article file SHA-256"):
                    collect(root, discovery, START, END, workers=1)
                fetch.assert_not_called()
            self.assertEqual((root / "manifest.json").read_bytes(), before)

    def test_source_and_document_hashes_are_independently_checked(self):
        for field in ("source_html_sha256", "document_sha256"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                discovery = source(root, [candidate()])
                with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML):
                    manifest = collect(root, discovery, START, END, workers=1)
                manifest["articles"][0][field] = "a" * 64
                write_json(root / "manifest.json", manifest)
                with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
                    verify_frozen_articles(root)

    def test_discovery_reference_hash_is_checked_before_cli_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = source(root, [candidate()])
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML):
                collect(root, discovery, START, END, workers=1)
            (root / "discovery_manifest.json").write_bytes(b'{"changed":true}')
            with self.assertRaisesRegex(ValueError, "discovery manifest SHA-256"):
                verify_frozen_articles(root, verify_discovery_reference=True)

    def test_body_validation_rejects_empty_and_out_of_window(self):
        article = parse_article(URL, HTML)
        self.assertEqual(validate_article(article, URL, START, END)["publish_time"], "2026-10-14T12:30:00+08:00")
        for fields in ({"content": ""}, {"publish_time": ""}, {"publish_time": "2026-10-15 00:00:00"}):
            with self.assertRaises(ValueError):
                validate_article({**article, **fields}, URL, START, END)

    def test_api_contract_boundaries_reject_overlong_inputs_without_truncating(self):
        article = parse_article(URL, HTML)
        for fields in ({"title": "x" * 501}, {"title": "line one\nline two"}, {"content": "x" * 200001}):
            with self.subTest(fields=list(fields)), self.assertRaisesRegex(ValueError, "contract"):
                validate_article({**article, **fields}, URL, START, END)
        full = {**article, "title": "x" * 500, "content": "x" * 200000}
        self.assertEqual(validate_article(full, URL, START, END)["content"], full["content"])

    def test_daily_target_counts_unique_successes_and_stops_even_with_more_candidates(self):
        rows = [candidate(f"{i:05}") for i in range(6)]
        pages = ["", HTML, HTML, HTML.replace("真实新闻", "第二条独立新闻"), HTML, HTML]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.fetch_html", side_effect=pages) as fetch:
                manifest = collect(root, source(root, rows), END, END, workers=1,
                                   target_per_day=2, request_interval=0)
            self.assertEqual(fetch.call_count, 4)
            self.assertEqual(manifest["success"], 2)
            self.assertEqual(manifest["deficits_by_date"], {"2026-10-14": 0})
            self.assertTrue(manifest["quota_complete"])
            self.assertEqual(manifest["collection_status"], "target_met")
            self.assertEqual(manifest["requested_candidates"], 6)

    def test_one_full_date_does_not_prevent_collecting_another_real_date_with_wrong_hint(self):
        rows = [candidate("00001"), candidate("00002")]
        day_13 = HTML.replace("2026-10-14 12:30:00", "2026-10-13 12:30:00").replace("真实新闻", "前一天原文")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.fetch_html", side_effect=[HTML, day_13]):
                manifest = collect(root, source(root, rows), date(2026, 10, 13), END,
                                   workers=1, target_per_day=1, per_channel_day=1, request_interval=0)
            self.assertEqual(manifest["counts_by_date"], {"2026-10-13": 1, "2026-10-14": 1})
            self.assertTrue(manifest["quota_complete"])

    def test_filled_hint_queue_is_deferred_then_revisited_for_a_different_actual_date(self):
        rows = [{**candidate("00003"), "candidate_date": "2026-10-14", "feed_publish_time": ""},
                {**candidate("00002"), "candidate_date": "2026-10-14", "feed_publish_time": ""},
                {**candidate("00001"), "candidate_date": "2026-10-13", "feed_publish_time": ""}]
        fetched = []

        def load(url, timeout):
            fetched.append(url.rsplit("/", 1)[-1])
            if url.endswith("00001"):
                return ""  # The deficit hint group is exhausted without a body.
            if url.endswith("00002"):
                return HTML.replace("2026-10-14 12:30:00", "2026-10-13 12:30:00").replace("真实新闻", "真实日期为十三日")
            return HTML

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.fetch_html", side_effect=load):
                manifest = collect(root, source(root, rows), date(2026, 10, 13), END,
                                   workers=1, target_per_day=1, request_interval=0)
            self.assertEqual(fetched, ["20261014A00003", "20261014A00001", "20261014A00002"])
            self.assertEqual(manifest["counts_by_date"], {"2026-10-13": 1, "2026-10-14": 1})
            self.assertTrue(manifest["quota_complete"])

    def test_reaching_all_daily_targets_leaves_deferred_candidates_unfetched(self):
        rows = [{**candidate("00003"), "candidate_date": "2026-10-14", "feed_publish_time": ""},
                {**candidate("00002"), "candidate_date": "2026-10-14", "feed_publish_time": ""},
                {**candidate("00001"), "candidate_date": "2026-10-13", "feed_publish_time": ""}]

        def load(url, timeout):
            return (HTML.replace("2026-10-14 12:30:00", "2026-10-13 12:30:00").replace("真实新闻", "十三日新闻")
                    if url.endswith("00001") else HTML)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.fetch_html", side_effect=load) as fetch:
                manifest = collect(root, source(root, rows), date(2026, 10, 13), END,
                                   workers=1, target_per_day=1, request_interval=0)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(manifest["deferred_candidates_remaining"], 1)
            self.assertEqual(manifest["success"], 2)

    def test_deficits_are_success_counts_and_do_not_report_candidate_hint_as_publication(self):
        rows = [{**candidate(), "category": "public", "channels": ["public"],
                 "candidate_date": "2026-10-07", "feed_publish_time": ""}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external.json"
            payload = {"schema_version": 1, "start_date": "2026-10-07", "end_date": "2026-10-14",
                       "candidates": rows, "counts_by_date": {"2026-10-07": 100000}, "settings": {"source": "sitemap"}}
            write_json(external, payload)
            raw = external.read_bytes()
            discovery = load_external_discovery(external, root, START, END)
            external.write_bytes(b"changed after source was frozen")
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML):
                manifest = collect(root, discovery, START, END, target_per_day=1000, request_interval=0)
            self.assertEqual(manifest["counts_by_date"]["2026-10-07"], 0)
            self.assertEqual(manifest["counts_by_date"]["2026-10-14"], 1)
            self.assertEqual(manifest["deficits_by_date"]["2026-10-07"], 1000)
            self.assertEqual(manifest["deficits_by_date"]["2026-10-14"], 999)
            self.assertEqual(manifest["source_manifest_sha256"], sha256(raw))
            self.assertEqual((root / manifest["source_manifest"]).read_bytes(), raw)
            verify_frozen_articles(root, verify_discovery_reference=True)

    def test_seed_copies_verified_original_bytes_and_counts_toward_new_daily_target(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / "v1", Path(directory) / "v2"
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML):
                first = collect(old, source(old, [candidate()]), END, END, workers=1, request_interval=0)
            frozen = {path.relative_to(old): path.read_bytes() for path in old.rglob("*") if path.is_file()}
            seed_from_manifest(new, old / "manifest.json", END, END,
                               expected_sha256=sha256((old / "manifest.json").read_bytes()), target_per_day=2)
            self.assertEqual((new / first["articles"][0]["relative_path"]).read_bytes(),
                             (old / first["articles"][0]["relative_path"]).read_bytes())
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML.replace("真实新闻", "第二篇")) as fetch:
                manifest = collect(new, source(new, [candidate(), candidate("00002")]), END, END,
                                   target_per_day=2, request_interval=0)
                fetch.assert_called_once()
            self.assertEqual(manifest["success"], 2)
            self.assertEqual(manifest["cached"], 1)
            self.assertTrue(manifest["quota_complete"])
            self.assertEqual(frozen, {path.relative_to(old): path.read_bytes() for path in old.rglob("*") if path.is_file()})

    def test_seed_tampering_fails_before_new_output_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / "v1", Path(directory) / "v2"
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML):
                manifest = collect(old, source(old, [candidate()]), END, END, workers=1, request_interval=0)
            (old / manifest["articles"][0]["relative_path"]).write_bytes(b"tampered frozen file")
            with self.assertRaisesRegex(ValueError, "article file SHA-256"):
                seed_from_manifest(new, old / "manifest.json", END, END)
            self.assertFalse((new / "manifest.json").exists())

    def test_interruption_keeps_prior_batch_and_resume_only_fetches_missing_article(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = source(root, [candidate(), candidate("00002")])
            with patch("experiments.collect_incremental_news.fetch_html", side_effect=[HTML, KeyboardInterrupt]):
                with self.assertRaises(KeyboardInterrupt):
                    collect(root, discovery, END, END, workers=1, target_per_day=2, request_interval=0)
            self.assertEqual(json.loads((root / "manifest.json").read_bytes())["success"], 1)
            verify_frozen_articles(root, verify_discovery_reference=True)
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML.replace("真实新闻", "继续采集新闻")) as fetch:
                manifest = collect(root, discovery, END, END, workers=1, target_per_day=2, request_interval=0)
                fetch.assert_called_once()
            self.assertEqual(manifest["success"], 2)

    def test_interruption_between_article_file_and_manifest_recovers_only_journal_bound_bytes(self):
        from experiments import collect_incremental_news as module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = source(root, [candidate()])
            original_write = module.write_json

            def interrupt_commit(path, value):
                if path.name == "manifest.json" and value.get("success") == 1:
                    raise KeyboardInterrupt
                return original_write(path, value)

            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML), patch(
                    "experiments.collect_incremental_news.write_json", side_effect=interrupt_commit):
                with self.assertRaises(KeyboardInterrupt):
                    collect(root, discovery, END, END, workers=1, request_interval=0)
            self.assertTrue((root / "pending_articles.json").exists())
            self.assertEqual(json.loads((root / "manifest.json").read_bytes())["success"], 0)
            recover_pending_articles(root)
            self.assertEqual(json.loads((root / "manifest.json").read_bytes())["success"], 1)
            self.assertFalse((root / "pending_articles.json").exists())
            verify_frozen_articles(root, verify_discovery_reference=True)

    def test_appending_feed_discovery_pins_previous_source_instead_of_invalidating_old_articles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.CHANNELS", ("tech",)), patch(
                    "experiments.collect_incremental_news.fetch_feed", return_value=json_bytes({"code": 0, "data": []})):
                discovery = discover(root, END, END, pages_per_channel=1, interval=0)
                discovery["candidates"] = [candidate()]
                with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML):
                    collect(root, discovery, END, END, workers=1, request_interval=0)
                discover(root, END, END, pages_per_channel=2, interval=0)
            verify_frozen_articles(root, verify_discovery_reference=True)
            manifest = json.loads((root / "manifest.json").read_bytes())
            self.assertTrue(manifest["source_manifest"].startswith("source-discovery/"))

    def test_expanded_discovery_pages_are_bounded_and_checkpoint_each_completed_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("experiments.collect_incremental_news.CHANNELS", ("tech",)), patch(
                    "experiments.collect_incremental_news.fetch_feed", side_effect=[json_bytes({"code": 0, "data": []}), KeyboardInterrupt]):
                with self.assertRaises(KeyboardInterrupt):
                    discover(root, END, END, pages_per_channel=200, interval=0)
            saved = json.loads((root / "discovery_manifest.json").read_bytes())
            self.assertEqual(len(saved["feed_requests"]), 1)
            with self.assertRaises(ValueError):
                discover(root, END, END, pages_per_channel=1001, interval=0)

    def test_worker_and_quota_configuration_stays_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = source(root, [])
            for kwargs in ({"workers": 9}, {"target_per_day": 0}, {"target_per_day": 10001},
                           {"per_channel_day": 10001}, {"request_interval": -1}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    collect(root, discovery, END, END, **kwargs)
            manifest = collect(root, discovery, END, END, workers=8, target_per_day=1000,
                               per_channel_day=1000, request_interval=0)
            self.assertEqual(manifest["deficits_by_date"], {"2026-10-14": 1000})

    def test_request_limiter_spaces_request_starts(self):
        limiter = RequestLimiter(0.25)
        with patch("experiments.collect_incremental_news.time.monotonic", side_effect=[1.0, 1.0, 1.1, 1.25]), patch(
                "experiments.collect_incremental_news.time.sleep") as sleep:
            limiter.wait()
            limiter.wait()
            self.assertAlmostEqual(sleep.call_args.args[0], 0.15)

    def test_semantic_contract_drift_fails_without_resigning_the_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = source(root, [candidate()])
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML):
                collect(root, discovery, END, END, workers=1, request_interval=0)
            frozen = (root / "manifest.json").read_bytes()
            for kwargs in ({"target_per_day": 999}, {"per_channel_day": 1000}):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "collection contract changed"):
                    collect(root, discovery, END, END, request_interval=0, **kwargs)
                self.assertEqual((root / "manifest.json").read_bytes(), frozen)
            # Runtime capacity and pacing may change without changing meaning.
            with patch("experiments.collect_incremental_news.fetch_html") as fetch:
                updated = collect(root, discovery, END, END, workers=8, timeout=30, request_interval=0.01)
                fetch.assert_not_called()
            self.assertEqual(updated["collection_contract"], json.loads(frozen)["collection_contract"])

    def test_legacy_running_manifest_contract_is_derived_only_from_saved_parameters(self):
        historical = {"start_date": "2026-10-07", "end_date": "2026-10-14", "timezone": "Asia/Shanghai",
                      "target_per_day": 1000, "settings": {"per_channel_day": None},
                      "source_manifest_sha256": "a" * 64, "collection_status": "collecting"}
        args = {"start": START, "end": END, "target_per_day": 1000,
                "per_channel_day": None, "source_sha256": "a" * 64}
        self.assertEqual(collection_contract(historical, **args)["target_per_day"], 1000)
        for change in ({"target_per_day": 999}, {"per_channel_day": 1000}, {"source_sha256": "b" * 64}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "collection contract changed"):
                collection_contract(historical, **{**args, **change})
        del historical["target_per_day"]
        with self.assertRaisesRegex(ValueError, "contract is incomplete"):
            collection_contract(historical, **args)

    def test_seed_reentry_does_not_reset_collected_state_or_allow_target_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / "v1", Path(directory) / "v2"
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML):
                collect(old, source(old, [candidate()]), END, END, workers=1, request_interval=0)
            seed_from_manifest(new, old / "manifest.json", END, END)
            discovery = source(new, [candidate(), candidate("00002")])
            with patch("experiments.collect_incremental_news.fetch_html", return_value=HTML.replace("真实新闻", "第二篇")):
                collect(new, discovery, END, END, workers=1, request_interval=0)
            before = (new / "manifest.json").read_bytes()
            seed_from_manifest(new, old / "manifest.json", END, END)
            self.assertEqual((new / "manifest.json").read_bytes(), before)
            with self.assertRaisesRegex(ValueError, "contract target"):
                seed_from_manifest(new, old / "manifest.json", END, END, target_per_day=999)
            self.assertEqual((new / "manifest.json").read_bytes(), before)

    def test_cli_defaults_to_new_v2_directory_without_touching_frozen_v1(self):
        with patch("sys.argv", ["collect_incremental_news", "--discover-only"]), patch(
                "pathlib.Path.exists", return_value=False), patch(
                "experiments.collect_incremental_news.recover_pending_articles"), patch(
                "experiments.collect_incremental_news.verify_frozen_articles"), patch(
                "experiments.collect_incremental_news.discover",
                return_value={"counts_by_date": {}, "candidates": [], "failures": []}) as discovery:
            main()
        self.assertEqual(discovery.call_args.args[0].name, "2026-10-07_2026-10-14-v2")


if __name__ == "__main__":
    unittest.main()
