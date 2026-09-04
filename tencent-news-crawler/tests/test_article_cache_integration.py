from crawler.tencent_news import NewsArticle
from service.article_cache import ArticleCache
from service.ingest_repository import IngestRepository
from service.news_ingest_service import NewsIngestService
from service.news_qa_service import NewsQAService
from service.qa_repository import QARepository


class CountingCrawler:
    def __init__(self, article):
        self.article = article
        self.calls = 0

    def crawl(self, url):
        self.calls += 1
        return self.article


class RawClient:
    def create_news_collection(self, **kwargs):
        return {"collectionId": "raw-id", "results": {"insertLen": 1}}


class QAClient:
    def create_news_qa_collection(self, **kwargs):
        return {"collectionId": "qa-id"}


def test_raw_ingest_and_qa_generation_share_cached_article(tmp_path):
    url = "https://news.qq.com/rain/a/SHARED_CACHE"
    article = NewsArticle(
        url=url,
        title="共享缓存新闻",
        publish_time="2026-08-20 10:00:00",
        author="腾讯新闻",
        content="这是一段足够长的共享缓存新闻正文。" * 10,
    )
    crawler = CountingCrawler(article)
    db_path = str(tmp_path / "test.db")
    cache = ArticleCache(tmp_path / "articles")
    ingest_repository = IngestRepository(db_path)
    ingest_repository.mark_pending(url)

    raw_service = NewsIngestService(
        crawler=crawler,
        fastgpt_client=RawClient(),
        repository=ingest_repository,
        article_cache=cache,
    )
    raw_service.ingest_url(url)

    qa_service = NewsQAService(
        crawler=crawler,
        fastgpt_client=QAClient(),
        qa_repository=QARepository(db_path),
        ingest_repository=ingest_repository,
        article_cache=cache,
    )
    result = qa_service.generate_for_url(url)

    assert result["status"] == "submitted"
    assert crawler.calls == 1
