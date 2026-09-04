import pytest

from crawler.tencent_news import NewsArticle
from service.news_qa_service import NewsQAService
from service.qa_repository import QARepository


class FakeCrawler:
    def crawl(self, url):
        return NewsArticle(
            url=url,
            title="测试新闻",
            publish_time="2026-08-19 10:00:00",
            author="腾讯新闻",
            content="这是一段足够长的新闻正文。" * 10,
        )


class FakeFastGPTClient:
    def __init__(self):
        self.calls = []

    def create_news_qa_collection(self, **kwargs):
        self.calls.append(kwargs)
        return {"collectionId": "qa-collection-1"}


class FakeIngestRepository:
    def list_pending_qa(self, limit, max_retry_count):
        return [
            {
                "url": "https://news.qq.com/rain/a/QA001",
                "title": "测试新闻",
                "topic": "科技",
                "publish_time": "2026-08-19 10:00:00",
            }
        ][:limit]


def build_service(tmp_path):
    return NewsQAService(
        crawler=FakeCrawler(),
        fastgpt_client=FakeFastGPTClient(),
        qa_repository=QARepository(str(tmp_path / "test.db")),
        ingest_repository=FakeIngestRepository(),
    )


def test_generate_for_url_submits_and_then_skips(tmp_path):
    service = build_service(tmp_path)
    url = "https://news.qq.com/rain/a/QA001"

    first = service.generate_for_url(url, {"topic": "科技"})
    second = service.generate_for_url(url, {"topic": "科技"})

    assert first["status"] == "submitted"
    assert second["status"] == "skipped"
    assert len(service.fastgpt_client.calls) == 1
    assert service.fastgpt_client.calls[0]["extra_metadata"]["topic"] == "科技"


def test_generate_latest_summarizes_results(tmp_path):
    service = build_service(tmp_path)

    result = service.generate_latest(limit=1)

    assert result["total"] == 1
    assert result["submitted"] == 1
    assert result["failed"] == 0


def test_generate_latest_returns_empty_summary_when_nothing_is_pending(tmp_path):
    service = build_service(tmp_path)
    service.ingest_repository = type(
        "EmptyIngestRepository",
        (),
        {
            "list_pending_qa": (
                lambda self, limit, max_retry_count: []
            )
        },
    )()

    result = service.generate_latest(limit=20)

    assert result == {
        "total": 0,
        "submitted": 0,
        "skipped": 0,
        "failed": 0,
        "retry_exhausted": 0,
        "items": [],
        "message": "当前没有待生成的QA新闻。",
    }


def test_generate_for_url_records_failure(tmp_path):
    service = build_service(tmp_path)

    class FailingClient:
        def create_news_qa_collection(self, **kwargs):
            raise RuntimeError("模型失败")

    service.fastgpt_client = FailingClient()
    url = "https://news.qq.com/rain/a/FAILED"

    with pytest.raises(RuntimeError, match="模型失败"):
        service.generate_for_url(url)

    assert service.qa_repository.get_by_url(url)["status"] == "failed"


def test_generate_for_url_does_not_retry_exhausted_failure(tmp_path):
    service = build_service(tmp_path)
    url = "https://news.qq.com/rain/a/EXHAUSTED"
    service.qa_repository.mark_pending(url)
    for index in range(3):
        service.qa_repository.mark_failed(url, f"失败 {index}")
        if index < 2:
            service.qa_repository.mark_pending(url)

    result = service.generate_for_url(url)

    assert result["status"] == "retry_exhausted"
    assert len(service.fastgpt_client.calls) == 0
