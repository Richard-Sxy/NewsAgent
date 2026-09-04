from crawler.topic_crawler import NewsIndex
from service.discovery_ingest_service import DiscoveryIngestService


class FakeTopicCrawler:
    def get_news_indexes(
        self,
        url,
        topic,
        limit,
        target_date=None,
        max_pages=3,
    ):
        if topic == "失败分类":
            raise RuntimeError("页面不可用")
        return [
            NewsIndex(
                "新闻一",
                "https://news.qq.com/rain/a/ONE",
                topic,
                "2026-08-19 10:00:00",
            ),
            NewsIndex(
                "新闻二",
                "https://news.qq.com/rain/a/TWO",
                topic,
                "2026-08-18 10:00:00",
            ),
        ][:limit]


class FakeIngestService:
    def __init__(self):
        self.calls = []
        self.repository = FakeRepository()

    def ingest_urls(self, urls, max_items, metadata_by_url=None):
        self.calls.append((urls, max_items, metadata_by_url))
        return {"total": len(urls), "succeeded": len(urls)}


class FakeRepository:
    def __init__(self):
        self.discovered = []

    def mark_discovered(self, **kwargs):
        self.discovered.append(kwargs)


def test_discover_deduplicates_across_topics_and_keeps_failures():
    service = DiscoveryIngestService(
        topic_crawler=FakeTopicCrawler(),
        ingest_service=FakeIngestService(),
    )
    result = service.discover(
        {
            "科技": "https://example.com/tech",
            "体育": "https://example.com/sports",
            "失败分类": "https://example.com/fail",
        },
        limit_per_topic=2,
    )

    assert result["discovered"] == 4
    assert result["unique"] == 2
    assert result["sources"][2]["status"] == "failed"


def test_dry_run_does_not_ingest():
    ingest_service = FakeIngestService()
    service = DiscoveryIngestService(
        topic_crawler=FakeTopicCrawler(),
        ingest_service=ingest_service,
    )

    result = service.discover_and_ingest(
        {"科技": "https://example.com/tech"},
        limit_per_topic=1,
        dry_run=True,
    )

    assert result["unique"] == 1
    assert result["ingest"] is None
    assert ingest_service.calls == []
    assert ingest_service.repository.discovered == []


def test_discover_and_ingest_passes_unique_urls():
    ingest_service = FakeIngestService()
    service = DiscoveryIngestService(
        topic_crawler=FakeTopicCrawler(),
        ingest_service=ingest_service,
    )

    result = service.discover_and_ingest(
        {"科技": "https://example.com/tech"},
        limit_per_topic=2,
    )

    assert result["ingest"]["succeeded"] == 2
    assert ingest_service.calls == [(
        [
            "https://news.qq.com/rain/a/ONE",
            "https://news.qq.com/rain/a/TWO",
        ],
        2,
        {
            "https://news.qq.com/rain/a/ONE": {
                "topic": "科技",
                "source_title": "新闻一",
                "discovered_publish_time": "2026-08-19 10:00:00",
            },
            "https://news.qq.com/rain/a/TWO": {
                "topic": "科技",
                "source_title": "新闻二",
                "discovered_publish_time": "2026-08-18 10:00:00",
            },
        },
    )]
    assert ingest_service.repository.discovered == [
        {
            "url": "https://news.qq.com/rain/a/ONE",
            "source_title": "新闻一",
            "topic": "科技",
            "publish_time": "2026-08-19 10:00:00",
        },
        {
            "url": "https://news.qq.com/rain/a/TWO",
            "source_title": "新闻二",
            "topic": "科技",
            "publish_time": "2026-08-18 10:00:00",
        },
    ]


def test_discover_passes_target_date_and_exposes_publish_time():
    captured = {}

    class CapturingCrawler:
        def get_news_indexes(
            self,
            url,
            topic,
            limit,
            target_date=None,
            max_pages=3,
        ):
            captured["target_date"] = target_date
            captured["max_pages"] = max_pages
            return [NewsIndex(
                "当日新闻",
                "https://news.qq.com/rain/a/TODAY",
                topic,
                "2026-08-19 12:00:00",
            )]

    service = DiscoveryIngestService(
        topic_crawler=CapturingCrawler(),
        ingest_service=FakeIngestService(),
    )
    result = service.discover_and_ingest(
        {"科技": "https://example.com/tech"},
        target_date="2026-08-19",
        max_pages=5,
        dry_run=True,
    )

    assert captured["target_date"] == "2026-08-19"
    assert captured["max_pages"] == 5
    assert result["items"][0]["publish_time"] == (
        "2026-08-19 12:00:00"
    )
