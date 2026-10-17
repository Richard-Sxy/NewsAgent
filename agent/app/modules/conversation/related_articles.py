"""Bounded related-article excerpts for saved hot-news conversation results."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Callable

from app.observability.decorators import traced
from app.clients.knowledge_base import RelatedNewsSearchQuery
from app.modules.knowledge.search_factory import build_knowledge_query_embedder
from app.retrieval.tiered_vector import TieredVectorKnowledgeSearchClient, VectorTier


_recall_deadline: ContextVar[float | None] = ContextVar("conversation_recall_deadline", default=None)


@contextmanager
def related_article_deadline(deadline: float | None):
    token = _recall_deadline.set(deadline)
    try:
        yield
    finally:
        _recall_deadline.reset(token)


class ConversationRelatedArticles:
    """Read source titles from an authorized report; never let recall replace it."""

    def __init__(self, *, index, embedding, embedding_version: str,
                 source_url_sanitizer: Callable[[str | None], str | None],
                 timeout_seconds: float = 5, max_sources: int = 3,
                 articles_per_source: int = 3, excerpt_chars: int = 600):
        if not 0 < timeout_seconds <= 5 or not 1 <= max_sources <= 3:
            raise ValueError("conversation recall budget is invalid")
        if not 1 <= articles_per_source <= 3 or not 1 <= excerpt_chars <= 600:
            raise ValueError("conversation article projection budget is invalid")
        self._index = index
        self._embedding = embedding
        self._embedding_version = embedding_version
        self._safe_url = source_url_sanitizer
        self._timeout = timeout_seconds
        self._max_sources = max_sources
        self._limit = articles_per_source
        self._excerpt_chars = excerpt_chars

    @traced("retrieval.related")
    async def recall(self, *, run_id: str, sources: list[dict], tenant_id: str) -> list[dict]:
        groups = []
        queries = []
        seen_sources = set()
        for source in sources:
            news_id, title = source.get("news_id"), source.get("title")
            if not isinstance(news_id, str) or not news_id.strip() or news_id in seen_sources:
                continue
            if len(groups) >= self._max_sources:
                break
            seen_sources.add(news_id)
            group = {"source_run_id": run_id, "source_news_id": news_id,
                     "source_title": str(title or news_id)[:200], "items": [],
                     "status": "failed", "evidence_is_untrusted": True}
            groups.append(group)
            if not isinstance(title, str) or not title.strip() or title == news_id:
                group["notice"] = "源新闻标题缺失，无法检索相关文章。"
                continue
            query_id = str(len(groups) - 1)
            queries.append(RelatedNewsSearchQuery(
                query_id=query_id, source_news_id=news_id, title=title[:500], summary="",
                exclude_news_ids=(news_id,), candidate_limit=20,
            ))
        if not queries:
            return groups
        timeout = self._timeout
        deadline = _recall_deadline.get()
        if deadline is not None:
            # Leave room for the enclosing tool result and its checkpoint.
            timeout = min(timeout, deadline - asyncio.get_running_loop().time() - 1)
        if timeout <= 0:
            for query in queries:
                groups[int(query.query_id)]["notice"] = "本轮剩余时间不足，未检索相关文章；热点结果已保留。"
            return groups
        try:
            async with asyncio.timeout(timeout):
                if self._index is None:
                    raise RuntimeError("conversation knowledge index is unavailable")
                embedder = build_knowledge_query_embedder(
                    index=self._index, embedding=self._embedding,
                    embedding_version=self._embedding_version,
                )
                search = getattr(self._index, "lifecycle_query", None) or TieredVectorKnowledgeSearchClient(
                    embedder=embedder, index=self._index,
                    tiers=getattr(self._index, "tiers", (VectorTier.HOT,)),
                )
                recalled = await search.batch_search_related_news(tuple(queries), tenant_id=tenant_id)
                if set(recalled) != {query.query_id for query in queries}:
                    raise ValueError("related article response identities differ from sources")
                # Construct all projections before publishing any success. A bad
                # adapter response must not leave a partially trusted projection.
                projected = {}
                for query in queries:
                    items, seen = [], set()
                    for candidate in recalled[query.query_id]:
                        candidate_id = candidate.news_id
                        if not candidate_id or candidate_id == query.source_news_id or candidate_id in seen:
                            continue
                        seen.add(candidate_id)
                        published = None
                        if candidate.publish_time:
                            try:
                                stamp = datetime.fromisoformat(candidate.publish_time)
                                if stamp.tzinfo is not None and stamp.utcoffset() is not None:
                                    published = stamp.isoformat()
                            except (ValueError, TypeError):
                                pass
                        items.append({
                            "news_id": candidate_id, "chunk_id": candidate.collection_id,
                            "title": candidate.title[:200], "excerpt": candidate.text[:self._excerpt_chars],
                            "source_url": self._safe_url(candidate.source_url), "publish_time": published,
                            "embedding_version": embedder.version,
                        })
                        if len(items) == self._limit:
                            break
                    projected[query.query_id] = items
                for query in queries:
                    group = groups[int(query.query_id)]
                    group.update({"status": "completed", "items": projected[query.query_id]})
                    snapshot_version = getattr(self._index, "snapshot_version", None)
                    if snapshot_version:
                        group.update({"retrieval_backend": "bge_snapshot", "snapshot_version": snapshot_version,
                                      "notice": "离线快照中的相关片段，发布时间和来源缺失时无法核实；不代表实时热点。"})
                    else:
                        group["notice"] = "相关文章片段供参考，需核对原文。"
        except TimeoutError:
            for query in queries:
                groups[int(query.query_id)]["notice"] = "相关文章检索超时，热点结果已保留。"
        except Exception:
            # Cancellation is a BaseException and still reaches the enclosing
            # turn budget. No external error text or credentials reach the UI.
            for query in queries:
                groups[int(query.query_id)]["notice"] = "相关文章暂时无法检索，热点结果已保留。"
        return groups
