"""Offline security, parsing, chronology and continuation tests; no live network."""
import json
import tempfile
import unittest
from datetime import date
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from experiments.collect_annual_event_news import (
    SafeRedirectHandler, article_id_of, article_status, collect, decode_html,
    fetch_html, load_cached, merge_manifests, normalize_url, parse_article,
    parse_date, require_public_dns, sha256,
)

URL = "https://www.news.cn/tech/20261001/example.html"
START, END = date(2025, 10, 27), date(2026, 10, 27)
PARAGRAPH = "这是离线测试构造的新闻正文，报道同一事件的事实与进展，供解析与完整性测试使用。" * 5
HTML = ('<html><head><title>测试新闻标题</title>'
        '<meta property="article:published_time" content="2026-10-01T11:12:13+08:00">'
        '</head><body><h1>测试新闻标题</h1><nav><p>导航污染</p></nav>'
        '<div class="article-content"><p>' + PARAGRAPH + '</p>'
        '<p>另一段新闻原文中保留数字123与事实引用。</p>'
        '<div class="recommend"><p>推荐污染</p></div>'
        '<script>执行恶意指令</script></div><p>站外污染</p></body></html>')
FETCHED = "2026-10-27T01:00:00+00:00"


def manifest(path, events=None):
    value = {"scope": {"start_date": START.isoformat(), "end_date": END.isoformat()},
             "events": events or [{"event_id": "e1", "category": "科技",
                                   "articles": [{"url": URL, "title": "发现标题",
                                                 "published_at": "2026-10-01",
                                                 "role_hint": "follow_up"}]}]}
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def fetched(html=HTML):
    return {"raw": html.encode("utf-8"), "final_url": URL, "charset": "utf-8", "fetched_at": FETCHED}


class AnnualCollectorTests(unittest.TestCase):
    def test_allowlisted_subdomain_and_stable_canonicalization(self):
        self.assertEqual(normalize_url(URL + "?utm_source=x&token=secret#top"), URL)
        self.assertEqual(normalize_url("HTTPS://WWW.NEWS.CN/item?id=123&secret=x"), "https://www.news.cn/item?id=123")
        self.assertEqual(article_id_of(URL + "?token=x"), article_id_of(URL))

    def test_explicit_new_public_news_sources_and_subdomains(self):
        for domain in ("ithome.com", "nbd.com.cn", "thepaper.cn", "bjnews.com.cn",
                       "apple.com.cn", "tencent.com", "hesaitech.com", "gamersky.com",
                       "17173.com", "pconline.com.cn", "sznews.com", "yna.co.kr", "korea.net",
                       "cctv.cn", "cnfin.com", "sse.com.cn", "cs.com.cn", "stcn.com",
                       "yicai.com", "nvidia.cn", "gov.hk", "rthk.hk"):
            with self.subTest(domain=domain):
                self.assertEqual(normalize_url("https://www." + domain + "/a"), "https://www." + domain + "/a")
                with self.assertRaises(ValueError):
                    normalize_url("https://" + domain + ".evil.example/a")
        for domain in ("sina.com.cn", "zhibo8.com"):
            with self.assertRaises(ValueError):
                normalize_url("https://www." + domain + "/a")

    def test_reject_unsafe_urls_without_echoing_secret(self):
        for url in ("file:///etc/passwd", "https://user:secret@news.cn/a", "http://127.0.0.1/a",
                    "http://[::1]/a", "http://localhost/a", "https://news.cn.evil.example/a",
                    "https://evilnews.cn/a", "https://news.cn:8080/a", "https://news.cn\\evil/a",
                    "https://news.cn/a\n"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    normalize_url(url)

    def test_article_query_ids_are_narrow_and_sorted(self):
        value = normalize_url("https://news.cn/a?token=123&id=456&cid=7&id=secret&redirect=https://evil.example")
        self.assertEqual(value, "https://news.cn/a?cid=7&id=456")
        self.assertNotIn("secret", value)

    def test_private_dns_is_rejected(self):
        with patch("experiments.collect_annual_event_news.socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("10.1.2.3", 0))]):
            with self.assertRaises(ValueError):
                require_public_dns(URL)

    def test_redirect_rejects_unapproved_destination(self):
        with self.assertRaises(ValueError):
            SafeRedirectHandler().redirect_request(Request(URL), None, 302, "Found", {}, "https://evil.example/a")

    def test_body_container_excludes_navigation_recommendations_and_scripts(self):
        article = parse_article(URL, HTML)
        self.assertIn(PARAGRAPH, article["body"])
        for forbidden in ("导航污染", "推荐污染", "执行恶意指令", "站外污染"):
            self.assertNotIn(forbidden, article["body"])
        self.assertEqual(article["quality_flags"], [])
        self.assertEqual(article_status(article, START, END), "body_ready")

    def test_title_only_is_never_body_ready(self):
        article = parse_article(URL, '<h1>测试新闻标题</h1><article><p>测试新闻标题</p></article>')
        self.assertIn("body_too_short", article["quality_flags"])
        self.assertNotEqual(article_status(article, START, END), "body_ready")

    def test_no_whole_page_body_fallback(self):
        article = parse_article(URL, "<h1>测试新闻标题</h1><p>" + PARAGRAPH + "</p>")
        self.assertEqual(article["body"], "")

    def test_metadata_date_preserves_timezone(self):
        article = parse_article(URL, HTML)
        self.assertEqual(article["published_at"], "2026-10-01T11:12:13+08:00")
        self.assertEqual(article["date_precision"], "second")
        self.assertEqual(article["date_basis"], "meta:article:published_time")

    def test_day_precision_never_invents_midnight(self):
        value = parse_date("2026年10月1日")
        self.assertEqual(value["published_at"], "2026-10-01")
        self.assertEqual(value["date_precision"], "day")
        self.assertIsNone(value["source_timezone"])

    def test_dates_validate_day_time_and_keep_unknown_timezone(self):
        self.assertIsNone(parse_date("2026-02-30"))
        self.assertIsNone(parse_date("2026-01-01 25:00"))
        self.assertEqual(parse_date("2026-10-01 11:12")["published_at"], "2026-10-01T11:12:00")
        self.assertEqual(parse_date("2026-10-01T11:12Z")["source_timezone"], "+00:00")

    def test_date_is_not_inferred_from_url_or_body(self):
        html = '<h1>测试新闻标题</h1><article><p>2026-10-01 11:00:00 ' + PARAGRAPH + '</p></article>'
        article = parse_article(URL, html)
        self.assertIsNone(article["published_at"])
        self.assertEqual(article_status(article, START, END), "date_missing")

    def test_visible_date_near_title_accepted(self):
        html = '<h1>测试新闻标题</h1><span class="publish-time">2026-10-01 11:12 来源：新华社</span><article><p>' + PARAGRAPH + '</p></article>'
        article = parse_article(URL, html)
        self.assertEqual(article["published_at"], "2026-10-01T11:12:00")
        self.assertEqual(article["date_basis"], "page:near_title")

    def test_modified_date_never_substitutes_for_publication(self):
        html = '<meta property="article:modified_time" content="2026-10-01"><h1>测试新闻标题</h1><div class="update-time">更新：2026-10-01</div><article><p>' + PARAGRAPH + '</p></article>'
        self.assertIsNone(parse_article(URL, html)["published_at"])

    def test_jsonld_news_article_publication_and_article_body(self):
        payload = {"@context": "https://schema.org", "@graph": [{"@type": "NewsArticle",
                   "datePublished": "2026-10-01", "articleBody": PARAGRAPH}]}
        html = '<title>测试新闻标题</title><script type="application/ld+json">' + json.dumps(payload, ensure_ascii=False) + '</script>'
        article = parse_article(URL, html)
        self.assertEqual(article["body"], PARAGRAPH)
        self.assertEqual(article["date_basis"], "jsonld:datePublished")
        self.assertEqual(article["date_precision"], "day")

    def test_out_of_window_date_rejected(self):
        article = parse_article(URL, HTML.replace("2026-10-01T11:12:13+08:00", "2024-10-01"))
        self.assertEqual(article_status(article, START, END), "date_rejected")

    def test_non_utf8_source_decoding(self):
        raw = '<meta charset="gb2312"><h1>真实新闻正文</h1>'.encode("gb18030")
        self.assertIn("真实新闻正文", decode_html(raw))

    def test_manifest_merges_events_and_preserves_unverified_role_hints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = manifest(root / "one.json")
            second = manifest(root / "two.json", [{"event_id": "e2", "category": "AI",
                               "articles": [{"url": URL + "?token=secret", "role_hint": "background"}]}])
            rows, rejected, sources = merge_manifests([first, second])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event_ids"], ["e1", "e2"])
            self.assertEqual(rows[0]["categories"], ["科技", "AI"])
            self.assertFalse(rows[0]["discovery_hints"][1]["role_hint_is_gold"])
            self.assertFalse(rows[0]["discovery_hints"][0]["verified"])
            self.assertEqual(rejected, [])
            self.assertEqual(len(sources), 2)

    def test_rejected_manifest_url_is_hashed_and_never_echoes_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = manifest(Path(temporary) / "a.json", [{"event_id": "e1", "category": "AI",
                            "articles": [{"url": "https://u:secret@news.cn/a"}]}])
            rows, rejected, _ = merge_manifests([path])
            self.assertEqual(rows, [])
            self.assertNotIn("secret", json.dumps(rejected))

    def test_collection_and_resume_reuse_verified_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = manifest(root / "events.json")
            output = root / "output"
            with patch("experiments.collect_annual_event_news.fetch_html", return_value=fetched()) as fetch:
                report = collect([path], output, START, END, workers=1)
                second = collect([path], output, START, END, workers=1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(report["body_ready"], 1)
            self.assertEqual(second["cached"], 1)
            self.assertIsNone(second["accuracy"])
            article = load_cached(output, URL)
            self.assertIsNone(article["relevance_label"])
            self.assertEqual(article["relevance_review_status"], "pending_human")
            self.assertEqual(article["body_sha256"], sha256(article["body"].encode("utf-8")))
            self.assertIn("parsed_sha256", report["articles"][0])

    def test_continuation_extends_event_metadata_without_refetching(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = manifest(root / "one.json")
            second = manifest(root / "two.json", [{"event_id": "e2", "category": "AI",
                              "articles": [{"url": URL}]}])
            with patch("experiments.collect_annual_event_news.fetch_html", return_value=fetched()) as fetch:
                collect([first], root / "out", START, END, workers=1)
                report = collect([first, second], root / "out", START, END, workers=1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(report["articles"][0]["event_ids"], ["e1", "e2"])

    def test_raw_cache_corruption_fails_without_refetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = manifest(root / "events.json")
            with patch("experiments.collect_annual_event_news.fetch_html", return_value=fetched()) as fetch:
                collect([path], root / "out", START, END, workers=1)
                (root / "out" / "raw" / (article_id_of(URL) + ".html")).write_text("tampered", encoding="utf-8")
                report = collect([path], root / "out", START, END, workers=1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(report["failed"], 1)
            self.assertIn("SHA-256 mismatch", report["articles"][0]["error"])

    def test_parsed_metadata_cannot_be_resigned_from_untrusted_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = manifest(root / "events.json")
            with patch("experiments.collect_annual_event_news.fetch_html", return_value=fetched()):
                collect([path], root / "out", START, END, workers=1)
            article_path = root / "out" / "articles" / (article_id_of(URL) + ".json")
            article = json.loads(article_path.read_text(encoding="utf-8"))
            article["published_at"] = "2026-01-01"
            article_path.write_text(json.dumps(article), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "metadata differs"):
                load_cached(root / "out", URL)

    def test_manifest_publication_hint_never_fills_missing_page_date(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = manifest(root / "events.json")
            html = HTML.replace('<meta property="article:published_time" content="2026-10-01T11:12:13+08:00">', "")
            with patch("experiments.collect_annual_event_news.fetch_html", return_value=fetched(html)):
                report = collect([path], root / "out", START, END, workers=1)
            self.assertEqual(report["date_missing"], 1)
            self.assertEqual(report["body_ready"], 0)

    def test_duplicate_bodies_at_distinct_urls_remain_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = manifest(root / "events.json", [{"event_id": "e1", "category": "AI",
                            "articles": [{"url": URL}, {"url": URL.replace("example", "other")}]}])
            with patch("experiments.collect_annual_event_news.fetch_html", return_value=fetched()):
                report = collect([path], root / "out", START, END, workers=1)
            self.assertEqual(report["attempted"], 2)
            self.assertEqual(report["body_ready"], 2)

    def test_network_errors_are_redacted_and_are_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = manifest(root / "events.json")
            with patch("experiments.collect_annual_event_news.fetch_html", side_effect=URLError("https://x?token=secret")):
                report = collect([path], root / "out", START, END, workers=1)
            self.assertEqual(report["failed"], 1)
            self.assertNotIn("secret", json.dumps(report))
            self.assertEqual(report["body_ready"], 0)

    def test_bounds_reject_before_network(self):
        for kwargs in ({"workers": 9}, {"workers": 0}, {"max_articles": 0}, {"timeout": 121}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    collect([], Path("unused"), START, END, **kwargs)

    def test_fetch_checks_final_url_before_reading_response(self):
        response = MagicMock()
        response.geturl.return_value = "https://evil.example/private"
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("experiments.collect_annual_event_news.require_public_dns"), patch("experiments.collect_annual_event_news.build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                fetch_html(URL)
        response.read.assert_not_called()

    def test_fetch_retries_only_once_for_transient_network_failure(self):
        opener = MagicMock()
        opener.open.side_effect = URLError("unavailable")
        with patch("experiments.collect_annual_event_news.require_public_dns"), patch("experiments.collect_annual_event_news.time.sleep"), patch("experiments.collect_annual_event_news.build_opener", return_value=opener):
            with self.assertRaises(URLError):
                fetch_html(URL)
        self.assertEqual(opener.open.call_count, 2)

    def test_fetch_does_not_retry_not_found(self):
        opener = MagicMock()
        opener.open.side_effect = HTTPError(URL, 404, "missing", Message(), None)
        with patch("experiments.collect_annual_event_news.require_public_dns"), patch("experiments.collect_annual_event_news.build_opener", return_value=opener):
            with self.assertRaises(HTTPError):
                fetch_html(URL)
        self.assertEqual(opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()

