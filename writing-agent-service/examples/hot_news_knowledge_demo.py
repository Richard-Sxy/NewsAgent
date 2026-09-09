"""热点排行、真实新闻缓存和知识库关联检索联调脚本。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.analytics.hot_news_enrichment import EnrichedHotNews, HotNewsEnrichmentService
from app.analytics.entities import ContentType
from app.analytics.news_content import (
    InMemoryNewsContentRepository,
    NewsContent,
    NewsContentRepository,
    TencentNewsCacheRepository,
)
from app.clients.knowledge_base import (
    FastGPTKnowledgeSearchClient,
    KnowledgeSearchSettings,
    RelatedNews,
    RelatedNewsSearchQuery,
)
from examples.hot_news_demo import build_ranking, load_scenario


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = PROJECT_ROOT / "tencent-news-crawler" / "data" / "articles"


class LocalSearchableNewsRepository(NewsContentRepository, Protocol):
    def list_all(self) -> list[NewsContent]: ...


class LocalKnowledgeSearchClient:
    """离线联调用的字符二元组检索，仅用于验证系统连接关系。"""

    def __init__(self, repository: LocalSearchableNewsRepository) -> None:
        self.repository = repository

    async def search_related_news(
        self,
        query: str,
        *,
        exclude_news_id: str | None = None,
        limit: int = 5,
    ) -> list[RelatedNews]:
        query_terms = self._bigrams(query)
        candidates = []
        for content in self.repository.list_all():
            if content.news_id == exclude_news_id:
                continue
            terms = self._bigrams(f"{content.title} {content.summary}")
            union = query_terms | terms
            score = len(query_terms & terms) / len(union) if union else 0.0
            if score > 0:
                candidates.append((score, content))
        candidates.sort(key=lambda item: (-item[0], item[1].news_id))
        return [
            RelatedNews(
                collection_id=content.collection_id or f"local-{content.news_id}",
                news_id=content.news_id,
                title=content.title,
                text=content.summary,
                source_url=content.source_url,
                publish_time=content.publish_time.isoformat(),
                score=round(score, 4),
            )
            for score, content in candidates[:limit]
        ]

    async def batch_search_related_news(
        self,
        queries: tuple[RelatedNewsSearchQuery, ...],
        *,
        tenant_id: str,
    ) -> dict[str, list[RelatedNews]]:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        results: dict[str, list[RelatedNews]] = {}
        for query in queries:
            query.validate()
            excluded = set(query.exclude_news_ids)
            excluded.add(query.source_news_id)
            candidates = await self.search_related_news(
                query.query_text,
                exclude_news_id=query.source_news_id,
                limit=query.candidate_limit + len(excluded),
            )
            results[query.query_id] = [
                item for item in candidates if item.news_id not in excluded
            ][: query.candidate_limit]
        return results

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        normalized = "".join(text.lower().split())
        return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def render_results(results: list[EnrichedHotNews]) -> str:
    lines = ["热点新闻与关联报道", "=" * 60]
    for item in results:
        current = item.ranking.current
        lines.append(
            f"{item.ranking.rank}. [{current.news_id}] "
            f"热点分数={item.ranking.hot_score.score:.4f}"
        )
        if item.content is None:
            lines.append("   内容：未找到")
            continue
        lines.append(f"   标题：{item.content.title}")
        lines.append(f"   原文：{item.content.source_url}")
        if not item.related_news:
            lines.append("   关联报道：未召回")
            continue
        lines.append("   关联报道：")
        for index, reranked in enumerate(item.related_news, start=1):
            related = reranked.news

            lines.append(f"     {index}. [{related.news_id or '-'}] {related.title}")
            lines.append(
                f"        最终分数={reranked.final_score:.4f} "
                f"向量={reranked.vector_score:.4f} "
                f"主体={reranked.entity_score:.4f} "
                f"事件={reranked.event_score:.4f}"
            )
            lines.append(
                f"        原因={','.join(reranked.reasons) or '无明确加分原因'} "
                f"来源={related.source_url or '-'}"
            )
    return "\n".join(lines)


async def run(
    *,
    offline: bool,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> list[EnrichedHotNews]:
    scenario = load_scenario()
    ranked = build_ranking(scenario)
    if cache_dir.is_dir():
        content_repository: LocalSearchableNewsRepository = (
            TencentNewsCacheRepository(cache_dir)
        )
    elif offline and cache_dir == DEFAULT_CACHE_DIR:
        # 仓库默认不提交爬虫缓存；离线演示回退到版本化场景数据。
        content_repository = InMemoryNewsContentRepository(
            [
                NewsContent(
                    news_id=str(item["news_id"]),
                    title=str(item["title"]),
                    summary=str(item["summary"]),
                    body=str(item["summary"]),
                    content_type=ContentType(str(item["content_type"])),
                    publish_time=datetime.fromisoformat(str(item["publish_time"])),
                    source_url=str(item["source_url"]),
                )
                for item in scenario["news"]
            ]
        )
    else:
        content_repository = TencentNewsCacheRepository(cache_dir)

    if offline:
        service = HotNewsEnrichmentService(
            content_repository,
            LocalKnowledgeSearchClient(content_repository),
        )
        return await service.enrich(
            ranked,
            tenant_id="local-demo",
            related_limit=3,
        )

    search_client = FastGPTKnowledgeSearchClient(KnowledgeSearchSettings())
    try:
        service = HotNewsEnrichmentService(content_repository, search_client)
        return await service.enrich(
            ranked,
            tenant_id="local-demo",
            related_limit=3,
        )
    finally:
        await search_client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="使用本地词面检索验证链路，不请求 FastGPT",
    )
    args = parser.parse_args()
    print(render_results(asyncio.run(run(offline=args.offline))))


if __name__ == "__main__":
    main()
