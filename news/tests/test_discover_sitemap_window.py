"""Offline sitemap allowlist, immutable cache and date-hint checks."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request

from experiments.discover_sitemap_window import (
    INDEX_URL, MAX_BYTES, SitemapRedirect, article_candidates, check_cache,
    digest, discover, fetch_xml, sitemap_date_hints, validate_sitemap_url, xml_locations,
)

START, END = date(2026, 10, 7), date(2026, 10, 8)
CHILD7 = "https://news.qq.com/sitemap/sitemap_1071500123.xml"
CHILD8 = "https://news.qq.com/sitemap/sitemap_1081500456.xml"
ARTICLE7 = "https://news.qq.com/rain/a/20261007A00001"
ARTICLE8 = "https://news.qq.com/rain/a/20261008A00001"


def xml(root, urls):
    parent, child = ("sitemap", "loc") if root == "sitemapindex" else ("url", "loc")
    return (f'<{root} xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
            "".join(f"<{parent}><{child}>{url}</{child}></{parent}>" for url in urls) + f"</{root}>").encode()


class DiscoverSitemapWindowTests(unittest.TestCase):
    def test_fixed_https_sitemap_allowlist(self):
        self.assertEqual(validate_sitemap_url(INDEX_URL), INDEX_URL)
        self.assertEqual(validate_sitemap_url(CHILD7), CHILD7)
        for invalid in (INDEX_URL.replace("https", "http"), INDEX_URL + "?x=1", INDEX_URL + "#a",
                        "https://news.qq.com.evil.test/sitemap/index.xml", "https://news.qq.com/sitemap/../../x.xml",
                        "https://u:p@news.qq.com/sitemap/index.xml", "https://news.qq.com:8443/sitemap/index.xml",
                        "https://news.qq.com/sitemap/sitemap_bad.xml"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_sitemap_url(invalid)

    def test_redirect_cannot_change_host_or_requested_object(self):
        handler = SitemapRedirect()
        for target in ("https://evil.test/sitemap/index.xml", CHILD7):
            with self.assertRaises(ValueError):
                handler.redirect_request(Request(INDEX_URL), None, 302, "Found", {}, target)

    def test_response_url_checked_before_bounded_read(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://evil.test/sitemap/index.xml"
        with patch("experiments.discover_sitemap_window.build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(ValueError):
                fetch_xml(INDEX_URL)
            response.read.assert_not_called()
        for raw in (b"", b"x" * (MAX_BYTES + 1)):
            response.geturl.return_value = INDEX_URL
            response.read.return_value = raw
            response.read.reset_mock()
            with patch("experiments.discover_sitemap_window.build_opener") as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(ValueError):
                    fetch_xml(INDEX_URL)
                response.read.assert_called_once_with(MAX_BYTES + 1)

    def test_html_challenges_and_entity_documents_are_rejected(self):
        for raw in (b"<html><script>challenge</script></html>",
                    b'<!DOCTYPE urlset [<!ENTITY test "expanded">]><urlset/>'):
            with self.assertRaises(ValueError):
                xml_locations(raw, "urlset")

    def test_filename_date_hints_unroll_month_from_fetch_reference(self):
        urls = ["https://news.qq.com/sitemap/sitemap_1011500123.xml",
                "https://news.qq.com/sitemap/sitemap_1301500123.xml",
                "https://news.qq.com/sitemap/sitemap_1291500123.xml"]
        rows = sitemap_date_hints(urls, date(2026, 10, 1))
        self.assertEqual([row["sitemap_date_hint"] for row in rows], ["2026-10-01", "2026-09-30", "2026-09-29"])
        self.assertEqual(rows[0]["minute"], 15 * 60)
        self.assertIsNone(sitemap_date_hints(["https://news.qq.com/sitemap/sitemap_1.xml"], END)[0]["sitemap_date_hint"])

    def test_A_ids_are_hints_and_video_outside_invalid_and_external_urls_excluded(self):
        urls = [ARTICLE7, ARTICLE7, ARTICLE8,
                "https://news.qq.com/rain/a/20261006A00001",  # previous-day hint retained
                "https://news.qq.com/rain/a/20261007V00001",
                "https://news.qq.com/rain/a/20261005A00001",
                "https://news.qq.com/rain/a/20261009A00001",
                "https://news.qq.com/rain/a/20261307A00001",
                "https://evil.test/rain/a/20261007A00001", ARTICLE7 + "?x=1"]
        found, rejected = article_candidates(xml("urlset", urls), date(2026, 10, 6), END)
        self.assertEqual(len(found), 3)
        self.assertEqual({row["candidate_date"] for row in found}, {"2026-10-06", "2026-10-07", "2026-10-08"})
        self.assertTrue(all(row["feed_publish_time"] == "" and row["date_hint_source"] == "news_id"
                            and row["category"] == "public" and row["channels"] == ["public"] for row in found))
        self.assertEqual(rejected["id_hint_outside_candidate_window"], 2)

    def test_discovery_meets_each_date_target_and_reuses_frozen_raw_cache(self):
        responses = {INDEX_URL: xml("sitemapindex", [CHILD8, CHILD7]),
                     CHILD7: xml("urlset", [ARTICLE7]), CHILD8: xml("urlset", [ARTICLE8])}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch("experiments.discover_sitemap_window.fetch_xml", side_effect=lambda url, timeout: responses[url]) as fetch:
                first = discover(output, START, END, max_sitemaps=2, workers=1, target_per_day=1)
                self.assertEqual(fetch.call_count, 3)
            with patch("experiments.discover_sitemap_window.fetch_xml") as fetch:
                second = discover(output, START, END, max_sitemaps=2, workers=1, target_per_day=1)
                fetch.assert_not_called()
            self.assertTrue(first["target_met"] and second["target_met"])
            self.assertEqual(first["counts_by_date"], {"2026-10-07": 1, "2026-10-08": 1})
            self.assertEqual(first["attempted_sitemaps"], 2)
            audit = json.loads((output / "source_manifest.json").read_bytes())
            self.assertEqual(audit["discovery_manifest_sha256"], digest((output / "discovery_manifest.json").read_bytes()))
            self.assertEqual(first["candidates"], second["candidates"])

    def test_tampered_raw_cache_blocks_all_requests_and_manifest_rewrite(self):
        responses = {INDEX_URL: xml("sitemapindex", [CHILD7]), CHILD7: xml("urlset", [ARTICLE7])}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch("experiments.discover_sitemap_window.fetch_xml", side_effect=lambda url, timeout: responses[url]):
                discover(output, START, START, max_sitemaps=1, workers=1, target_per_day=1)
            old_manifest = (output / "discovery_manifest.json").read_bytes()
            (output / "raw/sitemap_1071500123.xml").write_bytes(xml("urlset", []))
            with patch("experiments.discover_sitemap_window.fetch_xml") as fetch:
                with self.assertRaisesRegex(ValueError, "cache SHA-256"):
                    discover(output, START, START, max_sitemaps=1, target_per_day=1)
                fetch.assert_not_called()
            self.assertEqual((output / "discovery_manifest.json").read_bytes(), old_manifest)

    def test_budget_failure_is_reported_and_candidates_not_labeled_successful_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch("experiments.discover_sitemap_window.fetch_xml", side_effect=[xml("sitemapindex", [CHILD7]),
                                                                                    b"<html>WAF</html>"]):
                manifest = discover(output, START, START, max_sitemaps=1, workers=1, target_per_day=2000)
            self.assertFalse(manifest["target_met"])
            self.assertEqual(manifest["attempted_sitemaps"], 1)
            self.assertEqual(len(manifest["failures"]), 1)
            self.assertEqual(manifest["candidates"], [])
            self.assertNotIn("success", manifest)

    def test_cache_manifest_cannot_resign_tampered_raw_under_an_existing_source_audit(self):
        responses = {INDEX_URL: xml("sitemapindex", [CHILD7]), CHILD7: xml("urlset", [ARTICLE7])}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch("experiments.discover_sitemap_window.fetch_xml", side_effect=lambda url, timeout: responses[url]):
                discover(output, START, START, max_sitemaps=1, workers=1, target_per_day=1)
            raw = xml("urlset", [])
            (output / "raw/sitemap_1071500123.xml").write_bytes(raw)
            cache_path = output / "cache_manifest.json"
            cache = json.loads(cache_path.read_bytes())
            cache["records"][CHILD7].update({"sha256": digest(raw), "bytes": len(raw)})
            cache_path.write_text(json.dumps(cache))
            with patch("experiments.discover_sitemap_window.fetch_xml") as fetch:
                with self.assertRaisesRegex(ValueError, "cache_manifest SHA-256"):
                    discover(output, START, START, max_sitemaps=1, workers=1, target_per_day=1)
                fetch.assert_not_called()

    def test_modified_discovery_manifest_is_not_silently_rewritten(self):
        responses = {INDEX_URL: xml("sitemapindex", [CHILD7]), CHILD7: xml("urlset", [ARTICLE7])}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch("experiments.discover_sitemap_window.fetch_xml", side_effect=lambda url, timeout: responses[url]):
                discover(output, START, START, max_sitemaps=1, workers=1, target_per_day=1)
            path = output / "discovery_manifest.json"
            path.write_bytes(b'{"tampered":true}')
            with patch("experiments.discover_sitemap_window.fetch_xml") as fetch:
                with self.assertRaisesRegex(ValueError, "discovery_manifest SHA-256"):
                    discover(output, START, START, max_sitemaps=1, workers=1, target_per_day=1)
                fetch.assert_not_called()
            self.assertEqual(path.read_bytes(), b'{"tampered":true}')

    def test_budgets_are_validated_before_network(self):
        with tempfile.TemporaryDirectory() as directory, patch("experiments.discover_sitemap_window.fetch_xml") as fetch:
            for settings in ({"max_sitemaps": 1001}, {"workers": 9}, {"target_per_day": 0}, {"timeout": 0}):
                with self.assertRaises(ValueError):
                    discover(Path(directory), START, END, **settings)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
