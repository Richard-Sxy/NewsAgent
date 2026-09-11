from crawler.tencent_news import (
    NewsArticle,
    TencentNewsCrawler,
)
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from service.fastgpt_client import FastGPTClient
from config import settings
from service.article_cache import ArticleCache
from service.ingest_repository import IngestRepository
from intelligence.title_nlp import LTPTitleAnalyzer, TitleAnalysis

""" NewsArticle -> str 有标题/来源地址/发布消息/正文的转换 """
def format_article_text(article: NewsArticle) -> str:

    lines = [
        f"# {article.title}",
        "",
        f"来源地址: {article.url}",
    ]

    if article.author:
        lines.append(f"作者: {article.author}")
    
    if article.publish_time:
        lines.append(f"发布时间: {article.publish_time}")

    lines.extend([
        "",
        article.content,
    ])

    return "\n".join(lines)

""" 文章整理到知识库当中 """
class NewsIngestService:

    def __init__(
        self,
        crawler=None,
        fastgpt_client=None,
        repository=None,
        article_cache=None,
        title_analyzer=None,
    ):
        self.crawler = (
            crawler
            if crawler is not None
            else TencentNewsCrawler()
        )
        self.fastgpt_client = (
            fastgpt_client
            if fastgpt_client is not None
            else FastGPTClient()
        )
        self.repository = (
            repository
            if repository is not None
            else IngestRepository(
                settings.ingest_db_path
            )
        )
        cache_dir = (
            settings.article_cache_dir
            if repository is None
            else str(Path(self.repository.db_path).parent / "articles")
        )
        self.article_cache = article_cache or ArticleCache(cache_dir)
        self.title_analyzer = title_analyzer
        if self.title_analyzer is None and settings.title_nlp_enabled:
            self.title_analyzer = LTPTitleAnalyzer(
                model_name=settings.title_nlp_model,
                cache_dir=settings.title_nlp_cache_dir,
                local_files_only=settings.title_nlp_local_files_only,
                lexicon_path=settings.title_nlp_lexicon_path,
            )

    def _analyze_title(
        self,
        article: NewsArticle,
    ) -> TitleAnalysis | None:
        if self.title_analyzer is None:
            return None
        try:
            analysis = self.title_analyzer.analyze(article.title)
            self.repository.save_title_analysis(article.url, analysis)
            return analysis
        except Exception as exc:
            self.repository.mark_title_analysis_failed(
                article.url,
                article.title,
                str(exc),
                extractor=getattr(self.title_analyzer, "extractor", "unknown"),
                model_version=getattr(
                    self.title_analyzer, "model_version", "unknown"
                ),
            )
            if settings.title_nlp_required:
                raise RuntimeError(f"标题 NLP 抽取失败: {exc}") from exc
            return None
    
    """ 提取单次的URL """
    def ingest_url(
        self,
        url: str,
        extra_metadata: dict | None = None,
     ) -> dict:
        # SQLite 中去查重
        existing = self.repository.get_by_url(url)

        if existing and existing["status"] == "success":
            return {
                "url": url,
                "title": existing["title"],
                "collection_id": existing["collection_id"],
                "insert_len": 0,
                "status": "skipped",
            }
        if (
            existing
            and existing["status"] == "failed"
            and existing.get("retry_count", 0)
            >= settings.ingest_max_retry_count
        ):
            return {
                "url": url,
                "title": existing.get("title") or "",
                "collection_id": existing.get("collection_id"),
                "insert_len": 0,
                "status": "retry_exhausted",
                "error": (
                    existing.get("error_message")
                    or "重试次数已达上限"
                ),
            }

        self.repository.mark_pending(url)

        try:
            article, _ = self.article_cache.get_or_fetch(
                url,
                self.crawler.crawl,
            )

            title_analysis = self._analyze_title(article)

            text = format_article_text(article)

            metadata = dict(extra_metadata or {})
            if title_analysis is not None:
                # 可信的本地计算字段后写入，避免调用者覆盖分析结果。
                metadata.update(title_analysis.to_fastgpt_metadata())

            result = (
                self.fastgpt_client
                .create_news_collection(
                    article=article,
                    text=text,
                    extra_metadata=metadata,
                )
            )

            collection_id = result["collectionId"]

            self.repository.mark_success(
                url=article.url,
                title=article.title,
                collection_id=collection_id,
            )

            return {
                "url": article.url,
                "title": article.title,
                "collection_id": collection_id,
                "insert_len": result.get(
                    "results",
                    {},
                ).get("insertLen", 0),
                "status": "success",
            }

        except Exception as exc:
            self.repository.mark_failed(
                url=url,
                error_message=str(exc),
            )

            raise
    
    """ 批量提取 URL """
    def ingest_urls(
        self,
        urls: list[str],
        max_items: int = 20,
        metadata_by_url: dict[str, dict] | None = None,
        workers: int = 1,
    ) -> dict:
        metadata_by_url = metadata_by_url or {}

        unique_urls = list(
            dict.fromkeys(            # 这个函数是去重函数 保留原来顺序去掉后面的内容
                url.strip()
                for url in urls
                if url.strip()
            )
        )

        if not unique_urls:
            raise ValueError("新闻 URL 列表为空。")
        if len(unique_urls) > max_items:
            raise ValueError(
                f"单次最多导入 {max_items} 篇新闻。"
            )
        if workers < 1:
            raise ValueError("workers 必须大于 0。")
        
        items = []

        def ingest_one(url: str) -> dict:
            try:
                return self.ingest_url(
                    url,
                    extra_metadata=metadata_by_url.get(
                        url,
                        {},
                    )
                )
            except Exception as exc:
                return {
                    "url": url,
                    "title": "",
                    "collection_id": None,
                    "insert_len": 0,
                    "status": "failed",
                    "error": str(exc),
                }

        if workers == 1:
            items = [ingest_one(url) for url in unique_urls]
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                items = list(executor.map(ingest_one, unique_urls))
        
        # 这边做一个统计
        succeeded = sum(
            item["status"] == "success"
            for item in items
        )

        skipped = sum(
            item["status"] == "skipped"
            for item in items
        )

        failed = sum(
            item["status"] == "failed"
            for item in items
        )

        retry_exhausted = sum(
            item["status"] == "retry_exhausted"
            for item in items
        )

        return {
            "total": len(items),
            "succeeded": succeeded,
            "skipped": skipped,
            "failed": failed,
            "retry_exhausted": retry_exhausted,
            "items": items,
        }
