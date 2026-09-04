from crawler.topic_crawler import NewsIndex, TencentTopicCrawler
from service.news_ingest_service import NewsIngestService

"""发现并获取新闻内容服务"""
class DiscoveryIngestService:
    def __init__(self, topic_crawler=None, ingest_service=None):
        self.topic_crawler = topic_crawler or TencentTopicCrawler()
        self.ingest_service = ingest_service or NewsIngestService()

    """从源路径去发现固定数量的新闻内容"""
    def discover(
        self,
        sources: dict[str, str],
        limit_per_topic: int = 20,
        target_date: str | None = None,
        max_pages: int = 3,
    ) -> dict:
        if not sources:
            raise ValueError("至少需要一个新闻分类来源。")
        if limit_per_topic < 1:
            raise ValueError("每个分类的数量必须大于 0。")
        if max_pages < 1:
            raise ValueError("最大页数必须大于 0。")

        discovered: list[NewsIndex] = []
        source_results = []

        for topic, url in sources.items():
            try:
                items = self.topic_crawler.get_news_indexes(
                    url=url,
                    topic=topic,
                    limit=limit_per_topic,
                    target_date=target_date,
                    max_pages=max_pages,
                )
                discovered.extend(items)
                source_results.append({
                    "topic": topic,
                    "url": url,
                    "count": len(items),
                    "status": "success",
                })

            except Exception as exc:
                source_results.append({
                    "topic": topic,
                    "url": url,
                    "count": 0,
                    "status": "failed",
                    "error": str(exc),
                })

        unique_items = TencentTopicCrawler.deduplicate(discovered)
        return {
            "discovered": len(discovered),
            "unique": len(unique_items),
            "sources": source_results,
            "items": unique_items,
        }
    
    """ 发现并导入新闻 """
    def discover_and_ingest(
        self,
        sources: dict[str, str],
        limit_per_topic: int = 20,
        target_date: str | None = None,
        max_pages: int = 3,
        dry_run: bool = False,
    ) -> dict:
        discovery = self.discover(
            sources=sources,
            limit_per_topic=limit_per_topic,
            target_date=target_date,
            max_pages=max_pages,
        )
        items = discovery.pop("items")
        public_items = [
            {
                "topic": item.topic,
                "title": item.title,
                "url": item.url,
                "publish_time": item.publish_time,
            }
            for item in items
        ]

        if dry_run or not items:
            return {
                **discovery,
                "dry_run": dry_run,
                "ingest": None,
                "items": public_items,
            }
        
        for item in items:
            self.ingest_service.repository.mark_discovered(
                url=item.url,
                source_title=item.title,
                topic=item.topic,
                publish_time=item.publish_time,
            )

        metadata_by_url = {
            item.url: {
                "topic": item.topic,
                "source_title": item.title,
                "discovered_publish_time":(
                    item.publish_time
                ),
            }
            for item in items
        }

        ingest = self.ingest_service.ingest_urls(
            urls=[
                item.url 
                for item in items
            ],
            max_items=len(items),
            metadata_by_url=metadata_by_url,
        )
        return {
            **discovery,
            "dry_run": False,
            "ingest": ingest,
            "items": public_items,
        }
