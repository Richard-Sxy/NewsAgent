import unittest
from experiments.discover_annual_event_links import article_links, safe_url


class AnnualTopicDiscoveryTest(unittest.TestCase):
    def test_discovery_keeps_real_links_as_unverified_hints(self):
        html = '''<a href="/tech/20260325/abc/c.html">中关村论坛开幕</a>
        <a href="/tech/20260325/abc/c.html">重复链接</a>
        <a href="https://evil.test/20260325/abc/c.html">未批准来源新闻</a>
        <a href="/tech/20240325/old/c.html">不在时间范围</a>
        <a href="/tech/index.html">栏目首页</a>'''
        rows = article_links(html, "https://www.news.cn/topic/index.html",
            start="2025-10-27", end="2026-10-27", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "中关村论坛开幕")
        self.assertIsNone(rows[0]["published_at"])
        self.assertEqual(rows[0]["date_hint_from_url"], "2026-03-25")
        self.assertEqual(rows[0]["role_hint"], "unknown")

    def test_rejects_unreviewed_hosts_and_credentials(self):
        for url in ("http://127.0.0.1/a", "http://localhost/a", "https://www.news.cn.evil.test/a",
                    "https://user:pass@www.news.cn/a", "file:///tmp/news", "https://www.news.cn:9999/a"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                safe_url(url)

    def test_has_explicit_limit_and_does_not_create_links(self):
        html = ''.join('<a href="/20260325/id%d/c.html">真实链接标题%d</a>' % (i,i) for i in range(8))
        rows = article_links(html, "https://www.news.cn/index.html",
            start="2025-10-27", end="2026-10-27", limit=3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["discovered_from"] == "https://www.news.cn/index.html" for row in rows))


if __name__ == "__main__":
    unittest.main()
