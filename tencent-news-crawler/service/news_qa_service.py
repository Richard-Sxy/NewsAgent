from config import settings
from pathlib import Path
from crawler.tencent_news import TencentNewsCrawler
from service.article_cache import ArticleCache
from service.fastgpt_client import FastGPTClient
from service.ingest_repository import IngestRepository
from service.news_ingest_service import format_article_text
from service.qa_repository import QARepository

""" 创建QA服务 """
class NewsQAService:
    def __init__(
        self,
        crawler=None,
        fastgpt_client=None,
        qa_repository=None,
        ingest_repository=None,
        article_cache=None,
    ):
        self.crawler = crawler or TencentNewsCrawler()
        self.fastgpt_client = fastgpt_client or FastGPTClient()
        self.qa_repository = qa_repository or QARepository(
            settings.ingest_db_path
        )
        self.ingest_repository = ingest_repository or IngestRepository(
            settings.ingest_db_path
        )
        cache_dir = (
            settings.article_cache_dir
            if qa_repository is None
            else str(Path(self.qa_repository.db_path).parent / "articles")
        )
        self.article_cache = article_cache or ArticleCache(cache_dir)

    
    def generate_for_url(
        self,
        url: str,
        metadata: dict | None = None,
    ) -> dict:
        existing = self.qa_repository.get_by_url(url)
        if existing and existing["status"] == "submitted":
            return {
                "url": url,
                "status": "skipped",
                "collection_id": existing["qa_collection_id"],
            }
        if (
            existing
            and existing["status"] == "failed"
            and existing.get("retry_count", 0)
            >= settings.qa_max_retry_count
        ):
            return {
                "url": url,
                "status": "retry_exhausted",
                "collection_id": existing.get("qa_collection_id"),
                "error": (
                    existing.get("error_message")
                    or "重试次数已达上限"
                ),
            }

        self.qa_repository.mark_pending(url)
        try:
            article, _ = self.article_cache.get_or_fetch(
                url,
                self.crawler.crawl,
            )

            result = self.fastgpt_client.create_news_qa_collection(
                article=article,
                text=format_article_text(article),
                extra_metadata=metadata,
            )
            collection_id = result["collectionId"]
            self.qa_repository.mark_submitted(url, collection_id)
            return {
                "url": url,
                "title": article.title,
                "status": "submitted",
                "collection_id": collection_id,
            }
        except Exception as exc:
            self.qa_repository.mark_failed(url, str(exc))
            raise

    def generate_latest(self, limit: int = 3) -> dict:
        records = self.ingest_repository.list_pending_qa(
            limit=limit,
            max_retry_count=settings.qa_max_retry_count,
        )
        if not records:
            return {
                "total": 0,
                "submitted": 0,
                "skipped": 0,
                "failed": 0,
                "retry_exhausted": 0,
                "items": [],
                "message": "当前没有待生成的QA新闻。"
            }

        items = []
        for record in records:
            metadata = {
                "topic": record.get("topic") or "",
                "source_title": record.get("title") or "",
                "discovered_publish_time": record.get("publish_time") or "",
            }
            try:
                items.append(self.generate_for_url(record["url"], metadata))
            except Exception as exc:
                items.append({
                    "url": record["url"],
                    "status": "failed",
                    "error": str(exc),
                })

        return {
            "total": len(items),
            "submitted": sum(x["status"] == "submitted" for x in items),
            "skipped": sum(x["status"] == "skipped" for x in items),
            "failed": sum(x["status"] == "failed" for x in items),
            "retry_exhausted": sum(
                x["status"] == "retry_exhausted"
                for x in items
            ),
            "items": items,
        }
