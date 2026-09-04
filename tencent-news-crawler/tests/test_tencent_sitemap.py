from crawler.tencent_sitemap import TencentSitemapCrawler


def test_parse_locations_accepts_sitemap_namespaces():
    xml = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://news.qq.com/sitemap/a.xml</loc></sitemap>
      <sitemap><loc>https://news.qq.com/sitemap/b.xml</loc></sitemap>
    </sitemapindex>
    """
    assert TencentSitemapCrawler._parse_locations(xml) == [
        "https://news.qq.com/sitemap/a.xml",
        "https://news.qq.com/sitemap/b.xml",
    ]


def test_filter_article_urls_by_id_date_and_deduplicates():
    urls = [
        "https://news.qq.com/rain/a/20260903A001AA00",
        "https://news.qq.com/rain/a/20260904A001BB00",
        "https://news.qq.com/rain/a/20260903A001AA00",
        "https://example.com/rain/a/20260903A001CC00",
    ]
    assert TencentSitemapCrawler.filter_article_urls(urls, "2026-09-03") == [
        "https://news.qq.com/rain/a/20260903A001AA00",
    ]


def test_workers_and_limit_must_be_positive():
    try:
        TencentSitemapCrawler(workers=0)
    except ValueError as exc:
        assert "workers" in str(exc)
    else:
        raise AssertionError("workers=0 应当失败")

    crawler = TencentSitemapCrawler()
    try:
        crawler.discover("2026-09-03", limit=0)
    except ValueError as exc:
        assert "limit" in str(exc)
    else:
        raise AssertionError("limit=0 应当失败")
