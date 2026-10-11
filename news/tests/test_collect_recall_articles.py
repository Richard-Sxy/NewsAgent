"""Offline known-HTML, allowlist, cache and sampling checks; no network."""

import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request

from experiments.collect_recall_articles import (
    MAX_HTML_BYTES,
    PublicArticleRedirectHandler,
    collect_articles,
    fetch_html,
    normalize_article_url,
    parse_article,
    plan_urls,
    valid_cached_article,
)

URL = "https://news.qq.com/rain/a/20260820A0CSOK00"
HTML = """<html><head><title>备用标题_腾讯新闻</title>
<meta property="article:published_time" content="2026-08-20 19:17:00"></head>
<body><h1>新闻标题</h1><p>站外广告和导航</p><div id="article-content">
<p>第一段 <strong>真实正文</strong>。</p>
<!--NO_READ_BEGIN--><p>不可读广告</p><!--NO_READ_END-->
<p>责任编辑：张三</p><p>摄影 王五</p>
<p>记者：李四</p><p>记者了解到事情已经完成。</p><p>第二段正文。</p>
<script><p>恶意脚本</p></script></div></body></html>"""
FETCHED = "2026-10-06T02:00:00+00:00"


class CollectRecallArticlesTests(unittest.TestCase):
    def test_extract_only_article_paragraphs_and_remove_noise(self):
        article = parse_article(URL + "?from=test#top", HTML, FETCHED)
        self.assertEqual(article["url"], URL)
        self.assertEqual(article["news_id"], "20260820A0CSOK00")
        self.assertEqual(article["title"], "新闻标题")
        self.assertEqual(article["publish_time"], "2026-08-20 19:17:00")
        self.assertEqual(article["content"], "第一段 真实正文 。\n记者了解到事情已经完成。\n第二段正文。")
        self.assertEqual(article["content_version"], 1)
        self.assertEqual(article["fetched_at"], FETCHED)

    def test_supported_containers_and_title_fallback(self):
        for container in ('<div class="article-content">', '<div class="content-article">', '<article>'):
            closing = "</article>" if container == "<article>" else "</div>"
            with self.subTest(container=container):
                html = f"<title>备用标题_腾讯新闻</title>{container}<p>正文</p>{closing}"
                article = parse_article(URL, html, FETCHED)
                self.assertEqual(article["content"], "正文")
                self.assertEqual(article["title"], "备用标题")
                self.assertEqual(article["publish_time"], "")

    def test_recommendation_paragraphs_inside_container_are_excluded(self):
        html = ('<title>标题</title><article><p>真实正文</p>'
                '<aside><p>侧栏推荐</p></aside><div class="recommend-list"><p>推荐新闻</p></div>'
                '<div class="related-news"><p>相关内容推荐</p></div></article>'
                '<div><p>页尾推荐也不能进入正文</p></div>')
        self.assertEqual(parse_article(URL, html, FETCHED)["content"], "真实正文")

    def test_no_full_page_fallback_and_no_empty_body(self):
        invalid = ("", "<h1>标题</h1><p>站外导航不是正文</p>",
                   '<h1>标题</h1><article><div>没有段落</div></article>',
                   '<h1>标题</h1><article><p>责任编辑：张三</p></article>',
                   '<article><p>有正文但没有标题</p></article>')
        for html in invalid:
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_article(URL, html, FETCHED)

    def test_url_normalization_and_external_urls_rejected(self):
        self.assertEqual(normalize_article_url(URL + "/?from=question#x"), URL)
        invalid = (URL.replace("https:", "http:"), "https://other.qq.com/rain/a/123",
                   "https://news.qq.com.evil.test/rain/a/123", "https://news.qq.com/other/123",
                   "https://user:password@news.qq.com/rain/a/123",
                   "https://news.qq.com:8443/rain/a/123", "https://news.qq.com/rain/a/../../secret")
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_article_url(url)

    def test_external_redirect_rejected_before_following(self):
        handler = PublicArticleRedirectHandler()
        with self.assertRaises(ValueError):
            handler.redirect_request(Request(URL), None, 302, "Found", {},
                                     "https://evil.test/rain/a/123")

    def _response(self, raw, final_url=URL):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = final_url
        response.read.return_value = raw
        response.headers = Message()
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response

    def test_fetch_body_limit_empty_body_and_final_url_validation(self):
        for raw in (b"", b" " * 5, b"x" * (MAX_HTML_BYTES + 1)):
            response = self._response(raw)
            with self.subTest(length=len(raw)), patch(
                    "experiments.collect_recall_articles.build_opener") as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(ValueError):
                    fetch_html(URL)
                response.read.assert_called_once_with(MAX_HTML_BYTES + 1)
        response = self._response(b"body", "https://evil.test/rain/a/123")
        with patch("experiments.collect_recall_articles.build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(ValueError):
                fetch_html(URL)
            response.read.assert_not_called()

    def test_cache_is_reused_without_network_and_hash_is_frozen(self):
        article = parse_article(URL, HTML, FETCHED)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            path = out / f"{article['news_id']}.json"
            original = json.dumps(article, ensure_ascii=False)
            path.write_text(original, encoding="utf-8")
            with patch("experiments.collect_recall_articles.fetch_html") as fetch:
                first = collect_articles([URL], out)
                second = collect_articles([URL], out)
                fetch.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(first["cached"], 1)
            self.assertEqual(first["hash"], second["hash"])
            self.assertEqual(first["success"], 1)
            self.assertEqual(first["failures"], [])

    def test_success_and_failure_manifest_never_uses_answer_text(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            other = "https://news.qq.com/rain/a/other"
            with patch("experiments.collect_recall_articles.fetch_html", side_effect=[HTML, ""]):
                manifest = collect_articles([URL, other], out, workers=1)
            self.assertEqual(manifest["requested"], 2)
            self.assertEqual(manifest["success"], 1)
            self.assertEqual(manifest["failures"][0]["url"], other)
            self.assertIn("empty", manifest["failures"][0]["reason"])
            self.assertTrue((out / "collection_manifest.json").exists())
            self.assertFalse((out / "other.json").exists())

    def test_question_urls_and_distractors_are_deduplicated_and_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            questions = root / "questions.json"
            questions.write_text(json.dumps([{"expected_urls": [URL, URL + "?source=q"],
                                               "reference_answer": "不能用它生成正文"}]), encoding="utf-8")
            pool = root / "urls.txt"
            pool.write_text("\n".join([URL, "https://evil.test/rain/a/bad"] +
                                      [f"https://news.qq.com/rain/a/{i}" for i in range(20)]), encoding="utf-8")
            first, sources, rejected = plan_urls([questions], pool, 4, 42)
            second, _, _ = plan_urls([questions], pool, 4, 42)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 5)
            self.assertIn(URL, first)
            self.assertEqual(sources["target_urls"], 1)
            self.assertEqual(len(rejected), 1)
            with self.assertRaises(ValueError):
                plan_urls([questions], pool, 21, 42)

    def test_cache_contract_and_worker_bounds(self):
        article = parse_article(URL, HTML, FETCHED)
        self.assertTrue(valid_cached_article(article, URL))
        self.assertFalse(valid_cached_article({**article, "content": ""}, URL))
        self.assertFalse(valid_cached_article({**article, "news_id": "different"}, URL))
        self.assertFalse(valid_cached_article({**article, "fetched_at": "bad-date"}, URL))
        self.assertFalse(valid_cached_article({**article, "content_version": True}, URL))
        for workers in (0, 5, True):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                collect_articles([], Path("unused"), workers=workers)


if __name__ == "__main__":
    unittest.main()
