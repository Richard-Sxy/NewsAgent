"""Canonical active news for hotspot sources and related-news hydration.

The old crawler/content cache is deliberately absent from this adapter. The
final gate binds each source and candidate to the active immutable projection,
so a correction between recall and hydration cannot combine two revisions.
"""

from dataclasses import replace
from datetime import UTC

from sqlalchemy import and_, select

from app.analytics.entities import ContentType
from app.analytics.news_content import NewsContent
from app.modules.knowledge.tier_lifecycle_store import _jobs, _news


class LifecycleNewsContentRepository:
    lifecycle_guarded = True

    def __init__(self, *, database):
        self._database = database

    async def _active_rows(self, *, tenant_id, news_ids, include_body=False):
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        identities = tuple(dict.fromkeys(news_ids))
        rows = {}
        fields = [_news.c.news_id, _news.c.title, _news.c.source_url,
                  _news.c.publish_time, _news.c.active_job_id, _jobs.c.chunk_ids]
        if include_body:
            fields.append(_news.c.body)
        async with self._database.session() as session:
            for start in range(0, len(identities), 500):
                result = await session.execute(select(*fields).select_from(_news.join(_jobs,
                    and_(_news.c.active_job_id == _jobs.c.job_id,
                         _news.c.tenant_id == _jobs.c.tenant_id,
                         _news.c.source_revision == _jobs.c.source_revision))).where(
                    _news.c.tenant_id == tenant_id, _news.c.status == "active",
                    _news.c.news_id.in_(identities[start:start + 500])))
                for row in result.mappings():
                    rows[row["news_id"]] = dict(row)
        return rows

    @staticmethod
    def _publish_time(row):
        stamp = row["publish_time"]
        # Isolated SQLite tests drop tzinfo; production uses timestamptz.
        return stamp.replace(tzinfo=stamp.tzinfo or UTC)

    async def batch_get_by_news_ids(self, *, tenant_id, news_ids):
        rows = await self._active_rows(tenant_id=tenant_id, news_ids=news_ids, include_body=True)
        return {news_id: NewsContent(
            news_id=news_id, title=row["title"], summary=row["body"][:300],
            content_type=ContentType.ARTICLE, publish_time=self._publish_time(row),
            source_url=row["source_url"] or "", collection_id=row["active_job_id"],
            body=row["body"],
        ) for news_id, row in rows.items()}

    async def resolve_enrichment(self, *, tenant_id, sources, recalled):
        """Final serving gate after retrieval, before reranking/analysis.

        Sources carry their projection job ID in NewsContent.collection_id;
        candidates carry their immutable chunk ID in RelatedNews.collection_id.
        A changed projection is omitted, never repaired using a different
        version's title/body or a stale content-cache entry.
        """
        ids = list(sources)
        ids.extend(candidate.news_id for values in recalled.values() for candidate in values
                   if candidate.news_id)
        rows = await self._active_rows(tenant_id=tenant_id, news_ids=ids)
        surviving_sources = {
            news_id: content for news_id, content in sources.items()
            if news_id in rows and content.collection_id == rows[news_id]["active_job_id"]
        }
        resolved = {}
        manifests = {news_id: set(row["chunk_ids"]) for news_id, row in rows.items()}
        for query_id, candidates in recalled.items():
            values = []
            for candidate in candidates:
                row = rows.get(candidate.news_id)
                if row is None or candidate.collection_id not in manifests[candidate.news_id]:
                    continue
                values.append(replace(candidate, title=row["title"],
                    source_url=row["source_url"] or None,
                    publish_time=self._publish_time(row).isoformat()))
            resolved[query_id] = values
        return surviving_sources, resolved


def guard_run_dependencies(factory, *, content_repository):
    """Preserve run-scoped metrics/policy while enforcing canonical content."""
    if factory is None:
        return None

    async def build(request):
        dependencies = await factory(request)
        return replace(dependencies, content_repository=content_repository)

    return build
