"""Enterprise Port semantics backed by an explicitly disposable PostgreSQL DB."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row
import pytest
from sqlalchemy import create_engine

from app.clients.enterprise.common import EnterpriseRpcRequestError, EnterpriseRpcTimeoutError
from app.clients.knowledge_base import RelatedNewsSearchQuery
from simulation.streaming_lab.bridge import analyze_snapshot, SnapshotRunRequest, utc
from simulation.streaming_lab.dynamic_data import TENANTS
from simulation.streaming_lab.public_news import PublicContentRepository
from simulation.streaming_lab.sql_retrieval import SqlRelatedNewsSearchClient, title_terms
from tests.test_public_news_stream import START, snapshot


def test_terms_are_literal_and_bounded():
    assert title_terms('航天工程，航天！SQL 2026') == ['2026','sql','天工','工程','航天']
    assert title_terms("'; DROP TABLE users; --") == ['drop','table','users']
    assert title_terms('！') == []
    assert len(title_terms(' '.join(f'word{i}' for i in range(300)))) == 64


def test_retrieval_version_changes_run_identity_without_changing_old_identity():
    args=dict(tenant_id=TENANTS[0],window_start=utc(START),window_end=utc(START+60000),
              production_bundle_version='test-v2',snapshot_sha256='a'*64)
    old=SnapshotRunRequest(**args)
    new=SnapshotRunRequest(**args,retrieval_context_sha256='b'*64)
    assert old.idempotency_key != new.idempotency_key
    assert new.idempotency_key == SnapshotRunRequest(**args,retrieval_context_sha256='b'*64).idempotency_key
    with pytest.raises(ValueError):
        SnapshotRunRequest(**args,retrieval_context_sha256='invalid').validate()


@pytest.fixture(scope='module')
def sql_corpus():
    dsn=os.environ.get('SQL_RETRIEVAL_TEST_DSN')
    if not dsn:
        pytest.skip('explicit isolated PostgreSQL database required')
    assert urlparse(dsn).path.startswith('/newsagent_sql_test_'), 'Never initialize a user database'
    from app.modules.news_storage.model import build_package
    from app.modules.news_storage.store import FrozenNewsStore
    from app.modules.news_storage.tag_store import FrozenNewsTagStore
    from app.modules.news_storage.topic_labels import build_tag_package
    from tests.test_unified_news_tag_store import candidate, legacy
    from simulation.streaming_lab import public_news as public
    from simulation.streaming_lab.migrations import migrate
    engine=create_engine(dsn.replace('postgresql://','postgresql+psycopg://',1))
    content=FrozenNewsStore(engine);content.initialize()
    tags=FrozenNewsTagStore(content);tags.initialize()
    title='航天工程最新进展'
    observations=[candidate('a-source',title=title),candidate('zz-source-alias',title=title),
        candidate('b-related',title=title+'：空间站建设',body='航天工程空间站建设取得新进展。'*25),
        candidate('c-related',title=title+'：载人飞船',body='载人飞船航天工程研究。'*30),
        candidate('d-other',title='足球比赛结果',body='足球比赛体育新闻。'*30),
        candidate('e-future',title=title,body='未来航天工程。'*30,
                  publish_time=datetime.fromtimestamp((START+120000)/1000,timezone.utc).isoformat())]
    package=build_package(observations,tenant_id='public-test-retrieval')
    content.import_release(package)
    tagged=build_tag_package(package,legacy());tags.import_release(tagged)
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as db:
        db.execute(Path(os.environ['SQL_RETRIEVAL_TEST_INIT_SQL']).read_text())
        migrate(db);public.initialize_target(db)
        result=public.register_catalog(db,db,content_tenant=package['manifest']['tenant_id'],
            content_release=package['manifest']['release_id'],tag_release=tagged['manifest']['tag_release_id'])
        cat=public.load_catalog(db,result['catalog_id'],target=True)
    yield engine,cat
    engine.dispose()


def client(sql_corpus, tenant=TENANTS[0]):
    engine,cat=sql_corpus
    return SqlRelatedNewsSearchClient(engine,cat,tenant_id=tenant,as_of_ms=START+60000)


def query(**overrides):
    args=dict(query_id='q1',source_news_id='a-source',title='航天工程最新进展',summary='',candidate_limit=20)
    args.update(overrides)
    return RelatedNewsSearchQuery(**args)


def test_sql_recall_excludes_future_self_alias_and_explicit_exclusions(sql_corpus):
    search=client(sql_corpus)
    result=asyncio.run(search.batch_search_related_news((query(exclude_news_ids=('c-related',)),),tenant_id=TENANTS[0]))
    assert [c.news_id for c in result['q1']] == ['b-related']
    assert result['q1'][0].text and result['q1'][0].collection_id.startswith('sql-news-')
    audit=search.audit()
    assert audit['calls'][0]['results'][0]['candidates'][0]['news_id']=='b-related'
    assert audit['as_of_ms']==START+60000
    # Both tenants are explicitly granted this public corpus; context IDs stay separate.
    other=client(sql_corpus,TENANTS[1])
    assert other.identity_sha256 != search.identity_sha256


@pytest.mark.parametrize('kind',['foreign_tenant','other_granted_tenant','unknown_source','future_source','duplicate_query'])
def test_scope_failure_is_not_empty_success(sql_corpus,kind):
    search=client(sql_corpus)
    qs=(query(),);tenant=TENANTS[0]
    if kind=='foreign_tenant': tenant='foreign'
    elif kind=='other_granted_tenant': tenant=TENANTS[1]
    elif kind=='unknown_source': qs=(query(source_news_id='unknown'),)
    elif kind=='future_source': qs=(query(source_news_id='e-future'),)
    else: qs=(query(),query())
    with pytest.raises((EnterpriseRpcRequestError,ValueError)):
        asyncio.run(search.batch_search_related_news(qs,tenant_id=tenant))
    assert search.audit()['calls']==[]


def test_parameter_binding_empty_result_and_frozen_version_drift(sql_corpus):
    search=client(sql_corpus)
    result=asyncio.run(search.batch_search_related_news((query(title="'; DROP TABLE missing; --"),),tenant_id=TENANTS[0]))
    assert result == {'q1':[]}
    engine,cat=sql_corpus
    changed=deepcopy(cat);changed['manifest']['content_release_id']='wrong'
    wrong=SqlRelatedNewsSearchClient(engine,changed,tenant_id=TENANTS[0],as_of_ms=START+60000)
    with pytest.raises(ValueError,match='manifest drift'):
        asyncio.run(wrong.batch_search_related_news((query(),),tenant_id=TENANTS[0]))
    assert wrong.audit()['calls']==[]


def test_actual_sql_candidates_flow_through_analysis_whitelist(sql_corpus):
    engine,cat=sql_corpus
    body,payload,sid=snapshot(cat)
    search=client(sql_corpus)
    report=asyncio.run(analyze_snapshot(payload,sid,TENANTS[0],catalog=cat,
        content_repository=PublicContentRepository(cat,engine),related_search=search))
    assert report['production_bundle_version']=='streaming-public-retrieval-v2'
    assert report['retrieval']['calls'] and report['external_model_calls']==0
    assert len(report['analyses'])==3
    assert any(r['candidates'] for r in report['retrieval']['calls'][0]['results'])
    assert any(a['retrieval_evidence'] for a in report['analyses'])
    by_news={r['source_news_id']:{c['news_id'] for c in r['candidates']}
             for r in report['retrieval']['calls'][0]['results']}
    for analysis in report['analyses']:
        assert {e['news_id'] for e in analysis['retrieval_evidence']} <= by_news[analysis['news_id']]


def test_real_sql_deadline_does_not_return_empty_success(sql_corpus,monkeypatch):
    from sqlalchemy import text
    from simulation.streaming_lab import sql_retrieval
    search=client(sql_corpus)
    search._timeout_ms=200
    monkeypatch.setattr(sql_retrieval,'RECALL_SQL',text('SELECT pg_sleep(1)'))
    with pytest.raises(EnterpriseRpcTimeoutError):
        asyncio.run(search.batch_search_related_news((query(),),tenant_id=TENANTS[0]))
    assert search.audit()['calls']==[]
