from crawler.topic_crawler import (
    NewsIndex,
    TencentTopicCrawler,
)
import httpx
import pytest

def test_parse_news_indexes():
    html = """
    <html>
        <body>
            <a href="https://news.qq.com/rain/a/ARTICLE001">
                新闻一
            </a>

            <a href="//news.qq.com/rain/a/ARTICLE002">
                新闻二
            </a>

            <a href="/rain/a/ARTICLE003">
                新闻三
            </a>

            <a href="https://example.com/rain/a/ARTICLE004">
                外部网站
            </a>

            <a href="https://news.qq.com/other/page">
                非新闻正文
            </a>

            <a href="https://news.qq.com/rain/a/ARTICLE005">
            </a>
        </body>
    </html>
    """

    crawler = TencentTopicCrawler()
    results = crawler.parse_news_indexes(html, topic="科技")
    assert len(results) == 3
    assert results[0] == NewsIndex(
        title="新闻一",
        url="https://news.qq.com/rain/a/ARTICLE001",
        topic="科技",
    )
    assert results[1].url == (
        "https://news.qq.com/rain/a/ARTICLE002"
    )
    assert results[2].url == (
        "https://news.qq.com/rain/a/ARTICLE003"
    )

def test_deduplicate_news_indexes():
    """相同 URL 应只保留第一次出现的记录。"""
    indexes = [
        NewsIndex(
            title="第一次出现",
            url="https://news.qq.com/rain/a/SAME",
            topic="科技",
        ),
        NewsIndex(
            title="重复链接",
            url="https://news.qq.com/rain/a/SAME",
            topic="科技",
        ),
        NewsIndex(
            title="另一篇新闻",
            url="https://news.qq.com/rain/a/OTHER",
            topic="科技",
        ),
    ]

    results = TencentTopicCrawler.deduplicate(indexes)

    assert len(results) == 2
    assert results[0].title == "第一次出现"
    assert results[1].title == "另一篇新闻"

def test_get_news_indexes_calls_fetch_and_deduplicates(
    monkeypatch,
):
    """完整专题流程应执行下载、解析和去重。"""
    html = """
    <a href="/rain/a/SAME">新闻一</a>
    <a href="/rain/a/SAME">新闻一重复</a>
    """

    crawler = TencentTopicCrawler()

    def fake_fetch_html(url: str) -> str:
        assert url == "https://news.qq.com/tech"
        return html

    monkeypatch.setattr(
        crawler,
        "fetch_html",
        fake_fetch_html,
    )

    results = crawler.get_news_indexes(
        url="https://news.qq.com/tech",
        topic="科技",
    )

    assert len(results) == 1
    assert results[0].url == (
        "https://news.qq.com/rain/a/SAME"
    )
    assert results[0].topic == "科技"


def test_normalize_article_url_removes_query_and_fragment():
    result = TencentTopicCrawler.normalize_article_url(
        "http://news.qq.com/rain/a/ARTICLE001?from=tech#top"
    )
    assert result == "https://news.qq.com/rain/a/ARTICLE001"


def test_parse_news_indexes_finds_script_embedded_url():
    html = r'''<script>{"url":"https:\/\/news.qq.com\/rain\/a\/SCRIPT001"}</script>'''

    results = TencentTopicCrawler().parse_news_indexes(html, "科技")

    assert results == [
        NewsIndex(
            title="",
            url="https://news.qq.com/rain/a/SCRIPT001",
            topic="科技",
        )
    ]


def test_get_news_indexes_applies_limit(monkeypatch):
    crawler = TencentTopicCrawler()
    monkeypatch.setattr(
        crawler,
        "fetch_html",
        lambda url: """
            <a href='/rain/a/ONE'>一</a>
            <a href='/rain/a/TWO'>二</a>
        """,
    )

    results = crawler.get_news_indexes(
        "https://news.qq.com/ch/tech/",
        "科技",
        limit=1,
    )

    assert [item.url for item in results] == [
        "https://news.qq.com/rain/a/ONE"
    ]


def test_normalize_article_url_accepts_new_qq_domain():
    result = TencentTopicCrawler.normalize_article_url(
        "https://new.qq.com/rain/a/ARTICLE001"
    )
    assert result == "https://news.qq.com/rain/a/ARTICLE001"


def test_parse_feed_indexes_flattens_hot_items_and_filters_video():
    payload = {
        "data": [
            {
                "id": "HOT",
                "sub_item": [
                    {
                        "id": "UTR001",
                        "title": "热点文章",
                        "articletype": "116",
                        "publish_time": "2026-08-19 09:00:00",
                        "thing_info": {"focus_id": "ARTICLE001"},
                    }
                ],
            },
            {
                "id": "ARTICLE002",
                "title": "普通文章",
                "articletype": "0",
            },
            {
                "id": "VIDEO001",
                "title": "视频新闻",
                "articletype": "4",
            },
        ]
    }

    results = TencentTopicCrawler.parse_feed_indexes(payload, "科技")

    assert [item.url for item in results] == [
        "https://news.qq.com/rain/a/ARTICLE001",
        "https://news.qq.com/rain/a/ARTICLE002",
    ]
    assert results[0].publish_time == "2026-08-19 09:00:00"


def test_get_news_indexes_falls_back_to_feed(monkeypatch):
    crawler = TencentTopicCrawler()
    monkeypatch.setattr(
        crawler,
        "fetch_html",
        lambda url: '<script>window.channelInfo={"channelKey":"tech"}</script>',
    )
    captured = {"pages": []}

    def fake_fetch_feed(channel_key, limit, page=0):
        captured.update(channel_key=channel_key, limit=limit)
        captured["pages"].append(page)
        if page > 0:
            return {"data": []}
        return {
            "data": [{
                "id": "ARTICLE001",
                "title": "接口新闻",
                "articletype": "0",
            }]
        }

    monkeypatch.setattr(crawler, "fetch_feed", fake_fetch_feed)

    results = crawler.get_news_indexes(
        "https://news.qq.com/ch/tech/",
        "科技",
        limit=5,
    )

    assert captured == {
        "channel_key": "tech",
        "limit": 10,
        "pages": [0, 1],
    }
    assert results[0].title == "接口新闻"


def test_filter_by_date():
    indexes = [
        NewsIndex(
            "今天",
            "https://news.qq.com/rain/a/TODAY",
            "科技",
            "2026-08-19 08:30:00",
        ),
        NewsIndex(
            "昨天",
            "https://news.qq.com/rain/a/YESTERDAY",
            "科技",
            "2026-08-18 20:00:00",
        ),
        NewsIndex(
            "未知日期",
            "https://news.qq.com/rain/a/UNKNOWN",
            "科技",
        ),
    ]

    results = TencentTopicCrawler.filter_by_date(
        indexes,
        "2026-08-19",
    )

    assert [item.title for item in results] == ["今天"]


def test_get_news_indexes_filters_by_date_before_limit(monkeypatch):
    crawler = TencentTopicCrawler()
    monkeypatch.setattr(
        crawler,
        "fetch_html",
        lambda url: '<script>window.channelInfo={"channelKey":"tech"}</script>',
    )
    monkeypatch.setattr(
        crawler,
        "fetch_feed",
        lambda channel_key, limit, page=0: {
            "data": [
                {
                    "id": "OLD",
                    "title": "旧新闻",
                    "articletype": "0",
                    "publish_time": "2026-08-18 10:00:00",
                },
                {
                    "id": "TODAY",
                    "title": "今日新闻",
                    "articletype": "0",
                    "publish_time": "2026-08-19 10:00:00",
                },
            ]
        },
    )

    results = crawler.get_news_indexes(
        "https://news.qq.com/ch/tech/",
        "科技",
        limit=1,
        target_date="2026-08-19",
    )

    assert [item.title for item in results] == ["今日新闻"]


def test_get_news_indexes_collects_and_deduplicates_pages(monkeypatch):
    crawler = TencentTopicCrawler()
    monkeypatch.setattr(
        crawler,
        "fetch_html",
        lambda url: '<script>window.channelInfo={"channelKey":"tech"}</script>',
    )
    requested_pages = []

    def fake_fetch_feed(channel_key, limit, page=0):
        requested_pages.append(page)
        page_items = {
            0: [
                {
                    "id": "OLD",
                    "title": "昨天",
                    "articletype": "0",
                    "publish_time": "2026-08-18 10:00:00",
                },
                {
                    "id": "TODAY1",
                    "title": "今天一",
                    "articletype": "0",
                    "publish_time": "2026-08-19 10:00:00",
                },
            ],
            1: [
                {
                    "id": "TODAY1",
                    "title": "今天一重复",
                    "articletype": "0",
                    "publish_time": "2026-08-19 10:00:00",
                },
                {
                    "id": "TODAY2",
                    "title": "今天二",
                    "articletype": "0",
                    "publish_time": "2026-08-19 11:00:00",
                },
            ],
        }
        return {"data": page_items.get(page, [])}

    monkeypatch.setattr(crawler, "fetch_feed", fake_fetch_feed)

    results = crawler.get_news_indexes(
        "https://news.qq.com/ch/tech/",
        "科技",
        limit=2,
        target_date="2026-08-19",
        max_pages=3,
    )

    assert requested_pages == [0, 1]
    assert [item.title for item in results] == ["今天一", "今天二"]


def test_request_retries_network_error_then_succeeds(monkeypatch):
    crawler = TencentTopicCrawler()
    request = httpx.Request("GET", "https://news.qq.com/")
    calls = []
    delays = []

    def fake_request(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise httpx.ReadError("TLS EOF", request=request)
        return httpx.Response(200, request=request, text="ok")

    monkeypatch.setattr("crawler.topic_crawler.httpx.request", fake_request)
    monkeypatch.setattr(crawler, "wait_for_rate_limit", lambda: None)
    monkeypatch.setattr(
        "crawler.topic_crawler.time.sleep",
        lambda seconds: delays.append(seconds),
    )
    monkeypatch.setattr(
        "crawler.topic_crawler.settings.crawler_retry_base_delay",
        1.0,
    )

    response = crawler.request("GET", "https://news.qq.com/")

    assert response.text == "ok"
    assert len(calls) == 2
    assert delays == [1.0]


def test_request_retries_500_but_not_400(monkeypatch):
    crawler = TencentTopicCrawler()
    calls = []
    delays = []

    def fake_request(**kwargs):
        calls.append(kwargs)
        status = 500 if len(calls) == 1 else 400
        request = httpx.Request("GET", kwargs["url"])
        return httpx.Response(status, request=request)

    monkeypatch.setattr("crawler.topic_crawler.httpx.request", fake_request)
    monkeypatch.setattr(crawler, "wait_for_rate_limit", lambda: None)
    monkeypatch.setattr(
        "crawler.topic_crawler.time.sleep",
        lambda seconds: delays.append(seconds),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        crawler.request("GET", "https://news.qq.com/")

    assert exc_info.value.response.status_code == 400
    assert len(calls) == 2
    assert delays == [1.0]


def test_fetch_feed_uses_retrying_request(monkeypatch):
    crawler = TencentTopicCrawler()
    captured = {}
    request = httpx.Request(
        "POST",
        "https://i.news.qq.com/web_feed/getPCList",
    )

    def fake_request(**kwargs):
        captured.update(kwargs)
        return httpx.Response(
            200,
            request=request,
            json={"code": 0, "data": []},
        )

    monkeypatch.setattr(crawler, "request", fake_request)

    crawler.fetch_feed("tech", limit=10, page=2)

    assert captured["method"] == "POST"
    assert captured["json"]["flush_num"] == 2
    assert captured["json"]["forward"] == "1"


def test_later_page_network_error_preserves_previous_results(monkeypatch):
    crawler = TencentTopicCrawler()
    monkeypatch.setattr(
        crawler,
        "fetch_html",
        lambda url: '<script>window.channelInfo={"channelKey":"tech"}</script>',
    )
    request = httpx.Request(
        "POST",
        "https://i.news.qq.com/web_feed/getPCList",
    )

    def fake_fetch_feed(channel_key, limit, page=0):
        if page == 1:
            raise httpx.ReadError("TLS EOF", request=request)
        return {
            "data": [{
                "id": "FIRST",
                "title": "第一页新闻",
                "articletype": "0",
                "publish_time": "2026-08-19 10:00:00",
            }]
        }

    monkeypatch.setattr(crawler, "fetch_feed", fake_fetch_feed)

    results = crawler.get_news_indexes(
        "https://news.qq.com/ch/tech/",
        "科技",
        limit=2,
        target_date="2026-08-19",
        max_pages=3,
    )

    assert [item.title for item in results] == ["第一页新闻"]
