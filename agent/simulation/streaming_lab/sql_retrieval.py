"""Local SQL implementation of the enterprise retrieval Port, without model calls.

The catalog is a bounded, deduplicated public corpus. Business tenants receive an
explicit grant to that corpus, never access to another tenant's reader data.
"""
import asyncio
from datetime import datetime, timezone
import re
from time import monotonic

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.clients.enterprise.common import (
    EnterpriseRpcRequestError, EnterpriseRpcTimeoutError, RpcResponseMeta,
)
from app.clients.enterprise.related_news import (
    RelatedNewsBatchResponse, RelatedNewsCandidate, RelatedNewsQueryResult,
    RpcRelatedNewsSearchClient,
)
from app.observability.telemetry import operation
from simulation.streaming_lab.public_news import digest

POLICY = 'streaming-public-sql-lexical-v1'
MAX_CATALOG = 50000


def title_terms(title):
    """Literal Chinese bigrams and English/number words, with a fixed budget."""
    terms = set()
    for part in re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', title.lower()):
        if '\u4e00' <= part[0] <= '\u9fff':
            terms.update(part[i:i + 2] for i in range(len(part) - 1))
        else:
            terms.add(part)
    return sorted(terms)[:64]


RECALL_SQL = text('''
WITH scored AS (
  SELECT v.news_id,v.record_sha256,v.body_sha256,v.content_type,v.title,
         left(v.body,1000) AS excerpt,v.url,v.publish_time,c.publish_ms,
         (SELECT count(*)::float / :term_count
          FROM unnest(CAST(:terms AS text[])) AS q(term)
          WHERE strpos(lower(v.title),q.term)>0) AS score
  FROM news_realtime.catalog_news c
  JOIN public.news_content_release_items i
    ON i.tenant_id=:content_tenant AND i.release_id=:content_release
   AND i.news_id=c.news_id AND i.record_sha256=c.record_sha256
  JOIN public.news_content_versions v
    ON v.tenant_id=i.tenant_id AND v.news_id=i.news_id AND v.record_sha256=i.record_sha256
   AND v.body_sha256=c.body_sha256 AND v.content_type=c.content_type
  WHERE c.catalog_id=:catalog_id AND c.publish_ms<=:as_of_ms
    AND v.publish_time<=:as_of AND v.content_state='body_available'
    AND v.body_sha256<>:source_body_sha256
    AND NOT (v.news_id=ANY(CAST(:excluded AS text[])))
)
SELECT * FROM scored WHERE score>0
ORDER BY score DESC,publish_ms DESC,news_id COLLATE "C" LIMIT :candidate_limit
''')


class LocalSqlRelatedNewsRpc:
    """One read-only transaction per batch; SQL errors never become empty success."""

    def __init__(self, engine, catalog, *, tenant_id, as_of_ms):
        if (tenant_id not in catalog['manifest']['tenants']
                or type(as_of_ms) is not int or as_of_ms <= 0
                or not 1 <= len(catalog['news']) <= MAX_CATALOG):
            raise ValueError('Invalid retrieval tenant, time or catalog budget')
        self.engine, self.catalog = engine, catalog
        self.tenant_id, self.as_of_ms = tenant_id, as_of_ms
        self.as_of = datetime.fromtimestamp(as_of_ms / 1000, timezone.utc)
        self.index_version = 'sql-' + digest(dict(catalog_id=catalog['catalog_id'],
                                                 manifest=catalog['manifest'], policy=POLICY))
        self.identity_sha256 = digest(dict(tenant_id=tenant_id, as_of_ms=as_of_ms,
                                          index_version=self.index_version))
        self.calls = []

    async def batch_search_related_news(self, *, context, request):
        if context.tenant_id != self.tenant_id or request.retrieval_policy_version != POLICY:
            raise EnterpriseRpcRequestError('Retrieval scope/policy mismatch')
        ids = [q.query_id for q in request.queries]
        if len(set(ids)) != len(ids):
            raise EnterpriseRpcRequestError('Duplicate retrieval query identity')
        for query in request.queries:
            source = self.catalog['news'].get(query.source_news_id)
            if not source or source['publish_ms'] > self.as_of_ms:
                raise EnterpriseRpcRequestError('Retrieval source outside catalog/time scope')
        started = monotonic()
        try:
            async with asyncio.timeout(context.timeout_ms / 1000):
                response = await asyncio.to_thread(self._read, context, request, started)
        except TimeoutError as exc:
            raise EnterpriseRpcTimeoutError('Local SQL retrieval deadline exhausted',
                                            request_id=context.request_id) from exc
        except DBAPIError as exc:
            if getattr(exc.orig, 'sqlstate', None) == '57014':
                raise EnterpriseRpcTimeoutError('Local SQL statement deadline exhausted',
                                                request_id=context.request_id) from exc
            raise
        self.calls.append(dict(request_id=context.request_id, trace_id=context.trace_id,
            query_count=len(request.queries), elapsed_ms=round((monotonic()-started)*1000, 2),
            results=[dict(query_id=r.query_id, source_news_id=r.source_news_id,
                candidates=[dict(news_id=c.news_id, document_id=c.document_id,
                                 score=c.normalized_score) for c in r.candidates]) for r in response.results]))
        return response

    def _read(self, context, request, started):
        m = self.catalog['manifest']
        results = []
        with operation('streaming.sql.related_news', attributes={'newsagent.tenant_id':self.tenant_id}), self.engine.connect() as db, db.begin():
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            def deadline():
                remaining = context.timeout_ms - int((monotonic()-started)*1000)
                if remaining < 50:
                    raise EnterpriseRpcTimeoutError('SQL retrieval deadline exhausted')
                db.execute(text("SELECT set_config('statement_timeout',:ms,true)"), {'ms':str(remaining)})
            deadline()
            manifest = db.execute(text('SELECT manifest FROM news_realtime.catalog WHERE catalog_id=:id'),
                                  {'id':self.catalog['catalog_id']}).scalar_one_or_none()
            if manifest != m or self.catalog['catalog_id'] != 'public-'+digest(m)[:24]:
                raise ValueError('Retrieval catalog manifest drift')
            rows = db.execute(text('''SELECT news_id,content_type,record_sha256,body_sha256,publish_ms
                FROM news_realtime.catalog_news WHERE catalog_id=:id
                ORDER BY news_id COLLATE "C" LIMIT :budget'''),
                {'id':self.catalog['catalog_id'],'budget':MAX_CATALOG+1}).mappings().all()
            if len(rows) != m['eligible_count'] or digest([dict(r) for r in rows]) != m['rows_sha256']:
                raise ValueError('Retrieval catalog membership drift')
            binding = db.execute(text('''SELECT c.identity_sha256 AS content_sha,t.identity_sha256 AS tag_sha
                FROM public.news_content_releases c JOIN public.news_content_tag_releases t
                  ON t.tenant_id=c.tenant_id AND t.content_release_id=c.release_id
                WHERE c.tenant_id=:tenant AND c.release_id=:release AND t.tag_release_id=:tags
                  AND c.status='frozen' AND t.status='frozen' '''),
                {'tenant':m['content_tenant_id'],'release':m['content_release_id'],'tags':m['tag_release_id']}).mappings().one_or_none()
            if not binding or (binding['content_sha'],binding['tag_sha']) != (m['content_identity_sha256'],m['tag_identity_sha256']):
                raise ValueError('Retrieval release identity drift')
            for query in request.queries:
                deadline()
                terms = title_terms(query.title)
                rows = db.execute(RECALL_SQL, dict(terms=terms,term_count=max(1,len(terms)),
                    content_tenant=m['content_tenant_id'],content_release=m['content_release_id'],
                    catalog_id=self.catalog['catalog_id'],as_of_ms=self.as_of_ms,as_of=self.as_of,
                    source_body_sha256=self.catalog['news'][query.source_news_id]['body_sha256'],
                    excluded=list(dict.fromkeys((*query.exclude_news_ids,query.source_news_id))),
                    candidate_limit=query.candidate_limit)).mappings().all() if terms else []
                candidates = []
                for row in rows:
                    expected = self.catalog['news'].get(row['news_id'])
                    if not expected or any(row[k] != expected[k] for k in ('record_sha256','body_sha256','content_type','publish_ms')):
                        raise ValueError('Retrieval evidence outside frozen catalog')
                    candidates.append(RelatedNewsCandidate(news_id=row['news_id'],
                        document_id='sql-news-'+digest([self.index_version,row['news_id'],row['record_sha256']]),
                        title=row['title'][:500],excerpt=row['excerpt'],
                        source_url=row['url'] if row['url'] and len(row['url'])<=256 else None,
                        publish_time=row['publish_time'],normalized_score=row['score'],
                        retrieval_channel='local_sql_title_lexical',metadata_version=row['record_sha256']))
                results.append(RelatedNewsQueryResult(query_id=query.query_id,
                    source_news_id=query.source_news_id,candidates=tuple(candidates)))
        return RelatedNewsBatchResponse(results=tuple(results),retrieval_policy_version=POLICY,
            index_version=self.index_version,meta=RpcResponseMeta(request_id=context.request_id,
                source_system='local-public-news-postgresql',source_version=self.catalog['catalog_id'],
                served_at=datetime.now(timezone.utc)))


class SqlRelatedNewsSearchClient(RpcRelatedNewsSearchClient):
    def __init__(self, engine, catalog, *, tenant_id, as_of_ms):
        self.rpc = LocalSqlRelatedNewsRpc(engine,catalog,tenant_id=tenant_id,as_of_ms=as_of_ms)
        super().__init__(self.rpc,retrieval_policy_version=POLICY,timeout_ms=10000)
        self.identity_sha256 = self.rpc.identity_sha256

    def audit(self):
        return dict(mode='local_sql_enterprise_port',policy_version=POLICY,
            index_version=self.rpc.index_version,context_sha256=self.identity_sha256,
            tenant_id=self.rpc.tenant_id,as_of_ms=self.rpc.as_of_ms,
            catalog_id=self.rpc.catalog['catalog_id'],calls=list(self.rpc.calls),
            score_semantics='literal_title_term_overlap_not_embedding_similarity')
