import pytest

from crawler.tencent_news import NewsArticle
from service.ingest_repository import IngestRepository
from service.news_ingest_service import NewsIngestService


class FakeCrawler:

    def crawl(self, url: str) -> NewsArticle:
        return NewsArticle(
            url=url,
            title="测试新闻",
            publish_time="2026-08-17 10:00:00",
            author="腾讯新闻",
            content="这是一段足够长的测试正文。" * 10,
        )


class FakeFastGPTClient:

    def __init__(self):
        self.received_article = None
        self.received_text = None
        self.received_metadata = None

    def create_news_collection(
        self,
        article: NewsArticle,
        text: str,
        extra_metadata: dict | None = None,
    ) -> dict:
        self.received_article = article
        self.received_text = text
        self.received_metadata = extra_metadata

        return {
            "collectionId": "collection-test",
            "results": {
                "insertLen": 3,
            },
        }


def test_ingest_url(tmp_path):
    crawler = FakeCrawler()
    fastgpt_client = FakeFastGPTClient()
    repository = IngestRepository(
        str(tmp_path / "test.db")
    )

    service = NewsIngestService(
        crawler=crawler,
        fastgpt_client=fastgpt_client,
        repository=repository,
    )

    result = service.ingest_url(
        "https://news.qq.com/rain/a/test",
        extra_metadata={"topic": "科技"},
    )

    assert result == {
        "url": "https://news.qq.com/rain/a/test",
        "title": "测试新闻",
        "collection_id": "collection-test",
        "insert_len": 3,
        "status": "success",
    }

    assert "# 测试新闻" in (
        fastgpt_client.received_text
    )
    assert "来源地址:" in (
        fastgpt_client.received_text
    )
    assert "这是一段足够长的测试正文" in (
        fastgpt_client.received_text
    )
    assert fastgpt_client.received_metadata == {"topic": "科技"}

class ConfigurableCrawler:

    def __init__(self, article: NewsArticle):
        self.article = article

    def crawl(self, url: str) -> NewsArticle:
        return self.article

def test_ingest_url_rejects_empty_title(tmp_path):
    article = NewsArticle(
        url="https://news.qq.com/rain/a/test",
        title="",
        publish_time="",
        author="",
        content="正文内容" * 30,
    )

    repository = IngestRepository(
        str(tmp_path / "test.db")
    )

    service = NewsIngestService(
        crawler=ConfigurableCrawler(article),
        fastgpt_client=FakeFastGPTClient(),
        repository=repository,
    )

    with pytest.raises(
        ValueError,
        match="新闻标题为空",
    ):
        service.ingest_url(article.url)

def test_ingest_url_rejects_empty_content(tmp_path):
    article = NewsArticle(
        url="https://news.qq.com/rain/a/test",
        title="测试新闻",
        publish_time="",
        author="",
        content="",
    )

    repository = IngestRepository(
        str(tmp_path / "test.db")
    )

    service = NewsIngestService(
        crawler=ConfigurableCrawler(article),
        fastgpt_client=FakeFastGPTClient(),
        repository=repository,
    )

    with pytest.raises(
        ValueError,
        match="新闻正文为空",
    ):
        service.ingest_url(article.url)

def test_ingest_url_rejects_short_content(tmp_path):
    article = NewsArticle(
        url="https://news.qq.com/rain/a/test",
        title="测试新闻",
        publish_time="",
        author="",
        content="太短了",
    )

    service = NewsIngestService(
        crawler=ConfigurableCrawler(article),
        fastgpt_client=FakeFastGPTClient(),
        repository=IngestRepository(
            str(tmp_path / "test.db")
        ),
    )

    with pytest.raises(
        ValueError,
        match="新闻内容过短",
    ):
        service.ingest_url(article.url)

def test_ingest_url_skips_existing_success(tmp_path):
    repository = IngestRepository(
        str(tmp_path / "test.db")
    )

    url = "https://news.qq.com/rain/a/test"

    repository.mark_pending(url)
    repository.mark_success(
        url=url,
        title="已导入新闻",
        collection_id="existing-collection",
    )

    class MustNotCallCrawler:
        def crawl(self, url: str):
            raise AssertionError("不应该再次抓取")

    class MustNotCallFastGPT:
        def create_news_collection(
            self,
            article,
            text,
        ):
            raise AssertionError(
                "不应该再次调用 FastGPT"
            )

    service = NewsIngestService(
        crawler=MustNotCallCrawler(),
        fastgpt_client=MustNotCallFastGPT(),
        repository=repository,
    )

    result = service.ingest_url(url)

    assert result["status"] == "skipped"
    assert result["collection_id"] == (
        "existing-collection"
    )


def test_ingest_url_does_not_retry_exhausted_failure(tmp_path):
    repository = IngestRepository(str(tmp_path / "test.db"))
    url = "https://news.qq.com/rain/a/EXHAUSTED"
    repository.mark_pending(url)
    for index in range(3):
        repository.mark_failed(url, f"失败 {index}")
        if index < 2:
            repository.mark_pending(url)

    class MustNotCallCrawler:
        def crawl(self, url):
            raise AssertionError("达到上限后不应抓取")

    service = NewsIngestService(
        crawler=MustNotCallCrawler(),
        fastgpt_client=FakeFastGPTClient(),
        repository=repository,
    )

    result = service.ingest_url(url)

    assert result["status"] == "retry_exhausted"
    assert repository.get_by_url(url)["retry_count"] == 3

"""批量方法测试"""
def test_ingest_urls_collects_results(
    monkeypatch,
    tmp_path,
):
    repository = IngestRepository(
        str(tmp_path / "test.db")
    )

    service = NewsIngestService(
        crawler=FakeCrawler(),
        fastgpt_client=FakeFastGPTClient(),
        repository=repository,
    )

    def fake_ingest_url(url: str, extra_metadata=None) -> dict:
        if url.endswith("/failed"):
            raise RuntimeError("模拟失败")

        return {
            "url": url,
            "title": "测试新闻",
            "collection_id": "collection-test",
            "insert_len": 2,
            "status": "success",
        }

    monkeypatch.setattr(
        service,
        "ingest_url",
        fake_ingest_url,
    )

    result = service.ingest_urls([
        "https://news.qq.com/rain/a/success",
        "https://news.qq.com/rain/a/failed",
    ])

    assert result["total"] == 2
    assert result["succeeded"] == 1
    assert result["skipped"] == 0
    assert result["failed"] == 1

def test_ingest_urls_deduplicates_urls(
    monkeypatch,
    tmp_path,
):
    repository = IngestRepository(
        str(tmp_path / "test.db")
    )

    service = NewsIngestService(
        crawler=FakeCrawler(),
        fastgpt_client=FakeFastGPTClient(),
        repository=repository,
    )

    called_urls = []

    def fake_ingest_url(url: str, extra_metadata=None) -> dict:
        called_urls.append(url)

        return {
            "url": url,
            "title": "测试新闻",
            "collection_id": "collection-test",
            "insert_len": 1,
            "status": "success",
        }

    monkeypatch.setattr(
        service,
        "ingest_url",
        fake_ingest_url,
    )

    url = "https://news.qq.com/rain/a/test"

    result = service.ingest_urls([
        url,
        url,
        f"  {url}  ",
    ])

    assert result["total"] == 1
    assert called_urls == [url]


def test_ingest_urls_passes_metadata_by_url(monkeypatch, tmp_path):
    service = NewsIngestService(
        crawler=FakeCrawler(),
        fastgpt_client=FakeFastGPTClient(),
        repository=IngestRepository(str(tmp_path / "test.db")),
    )
    received = []

    def fake_ingest_url(url, extra_metadata=None):
        received.append((url, extra_metadata))
        return {
            "url": url,
            "title": "测试新闻",
            "collection_id": "collection-test",
            "insert_len": 1,
            "status": "success",
        }

    monkeypatch.setattr(service, "ingest_url", fake_ingest_url)
    url = "https://news.qq.com/rain/a/test"

    service.ingest_urls(
        [url],
        metadata_by_url={url: {"topic": "科技"}},
    )

    assert received == [(url, {"topic": "科技"})]

def test_ingest_urls_rejects_too_many_urls(
    tmp_path,
):
    service = NewsIngestService(
        crawler=FakeCrawler(),
        fastgpt_client=FakeFastGPTClient(),
        repository=IngestRepository(
            str(tmp_path / "test.db")
        ),
    )

    urls = [
        f"https://news.qq.com/rain/a/{index}"
        for index in range(21)
    ]

    with pytest.raises(
        ValueError,
        match="单次最多导入 20 篇新闻",
    ):
        service.ingest_urls(urls)
