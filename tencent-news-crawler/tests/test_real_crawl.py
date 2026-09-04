import pytest
from crawler.tencent_news import TencentNewsCrawler

@pytest.mark.integration
def test_real_crawl():
    """测试真实的爬取功能。"""
    url = "https://news.qq.com/rain/a/20230928A0B1C200"
    crawler = TencentNewsCrawler()
    article = crawler.crawl(url)

    assert article.url == url
    assert article.title
    assert article.content