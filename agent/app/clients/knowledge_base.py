"""
定义关联新闻检索接口。RelatedNewsSearchQuery 表示检索问题， Related 表示候选报道， KnowledgeSearchClient 规定批量检索方法。
之前看到的 retrieval 就实现了这个接口。
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RelatedNews:
    collection_id: str
    news_id: str | None
    title: str
    text: str
    source_url: str | None
    publish_time: str | None
    score: float | None


@dataclass(frozen=True, slots=True)
class RelatedNewsSearchQuery:
    """Stable domain query for one related-news recall."""

    query_id: str
    source_news_id: str
    title: str
    summary: str
    exclude_news_ids: tuple[str, ...] = ()
    candidate_limit: int = 20

    def validate(self) -> None:
        if not self.query_id.strip():
            raise ValueError("query_id cannot be empty")
        if not self.source_news_id.strip():
            raise ValueError("source_news_id cannot be empty")
        if not self.title.strip():
            raise ValueError("title cannot be empty")
        if self.candidate_limit <= 0:
            raise ValueError("candidate_limit must be greater than 0")
        if any(not item.strip() for item in self.exclude_news_ids):
            raise ValueError("exclude_news_ids cannot contain empty values")

    @property
    def query_text(self) -> str:
        return " ".join(
            part.strip()
            for part in (self.title, self.summary)
            if part.strip()
        )


class KnowledgeSearchClient(Protocol):
    async def batch_search_related_news(
        self,
        queries: tuple[RelatedNewsSearchQuery, ...],
        *,
        tenant_id: str,
    ) -> dict[str, list[RelatedNews]]: ...
