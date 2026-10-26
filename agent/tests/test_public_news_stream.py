"""Real-content identities, deterministic synthetic traffic and cross-DB SQL recovery."""
import asyncio
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest
from sqlalchemy import create_engine

from simulation.streaming_lab import public_news as public
from simulation.streaming_lab.bridge import analyze_snapshot, validate_payload
from simulation.streaming_lab.dynamic_data import TENANTS, encode
from tools.streaming_lab_acceptance import recount

START=1800000000000


def catalog():
    return {'catalog_id':'public-'+'a'*24,'manifest':{'tenants':list(TENANTS)},
            'news':{f'news-{i}':dict(news_id=f'news-{i}',content_type='article',publish_ms=START-1000,
                record_sha256=str(i)*64,body_sha256=str(i)*64) for i in range(1,8)}}


def snapshot(cat, sequence=1):
    value=public.public_packet(sequence,START,cat)
    rows,count=recount(value['events'],TENANTS[0])
    body=dict(tenant_id=TENANTS[0],run_id=value['run_id'],window_start_ms=START,window_end_ms=START+60000,
        metric_version=public.METRIC_VERSION,catalog_id=cat['catalog_id'],final=True,event_count=count,event_digest='b'*64,metrics=rows)
    text=encode(body).decode()
    return body,text,sha256(text.encode()).hexdigest()


def test_public_packets_rotate_real_ids_and_remain_replayable():
    cat=catalog()
    a=public.public_packet(1,START,cat)
    assert a==public.public_packet(1,START,cat)
    b=public.public_packet(2,START,cat)
    assert {e['news_id'] for e in a['events']}!={e['news_id'] for e in b['events']}
    assert all(e['schema_version']==2 and e['catalog_id']==cat['catalog_id'] for e in a['events'])
    assert len({e['event_id'] for e in a['events']})==len(a['events'])-12
    for tenant in TENANTS:
        rows,_=recount(a['events'],tenant)
        assert all(0<r['unique_users']<=r['clicks']<=r['impressions'] for r in rows)


@pytest.mark.parametrize('change',['missing_catalog','other_catalog','future_publication','unknown_news','wrong_type','duplicate_news'])
def test_public_snapshot_scope_and_publication_fail_closed(change):
    cat=catalog();body,text,sid=snapshot(cat)
    assert validate_payload(text,sid,TENANTS[0],catalog=cat)==body
    if change=='missing_catalog': cat=None
    elif change=='other_catalog': cat['catalog_id']='public-'+'c'*24
    elif change=='future_publication':
        cat['news'][body['metrics'][0]['news_id']]['publish_ms']=START+1
    elif change=='unknown_news': body['metrics'][0]['news_id']='unknown'
    elif change=='wrong_type': body['metrics'][0]['content_type']='video'
    else: body['metrics'].append(deepcopy(body['metrics'][0]))
    text=encode(body).decode();sid=sha256(text.encode()).hexdigest()
    with pytest.raises(ValueError): validate_payload(text,sid,TENANTS[0],catalog=cat)


def test_future_only_catalog_cannot_generate_events():
    cat=catalog()
    for row in cat['news'].values(): row['publish_ms']=START+1
    with pytest.raises(ValueError): public.public_packet(1,START,cat)


@pytest.mark.skipif(not os.environ.get('PUBLIC_NEWS_TEST_DSN'),reason='explicit isolated PostgreSQL database required')
def test_sql_dedup_content_join_projection_retry_and_invalidation():
    from urllib.parse import urlparse
    from app.modules.news_storage.model import build_package
    from app.modules.news_storage.store import FrozenNewsStore
    from app.modules.news_storage.tag_store import FrozenNewsTagStore
    from app.modules.news_storage.topic_labels import build_tag_package
    from tests.test_unified_news_tag_store import candidate, legacy
    from simulation.streaming_lab.migrations import migrate

    dsn=os.environ['PUBLIC_NEWS_TEST_DSN']
    assert urlparse(dsn).path.startswith('/newsagent_sql_test_'), 'Never run schema tests on a user database'
    engine=create_engine(dsn.replace('postgresql://','postgresql+psycopg://',1))
    content=FrozenNewsStore(engine);content.initialize()
    tags=FrozenNewsTagStore(content);tags.initialize()
    observations=[candidate('news-1'),candidate('news-1'),candidate('news-1-alias'),
        candidate('news-2',body='市场财经新闻。'*30),candidate('news-3',body='航天科技研究。'*30),
        candidate('title-only',body=None,content_state='title_only')]
    package=build_package(observations,tenant_id='public-test')
    assert package['manifest']['counts']['news_count']==5
    content.import_release(package)
    assert content.import_release(package)['import_status']=='already_present'
    tagged=build_tag_package(package,legacy());tags.import_release(tagged)
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as source, psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as target:
        source.execute(Path(os.environ['PUBLIC_NEWS_TEST_INIT_SQL']).read_text())
        migrate(source)
        public.initialize_target(target)
        scope=dict(content_tenant='public-test',content_release=package['manifest']['release_id'],tag_release=tagged['manifest']['tag_release_id'])
        registered=public.register_catalog(target,source,**scope)
        assert public.register_catalog(target,source,**scope)==registered
        cat=public.load_catalog(source,registered['catalog_id'])
        assert len(cat['news'])==3 and 'news-1-alias' not in cat['news']
        assert target.execute('SELECT count(*) AS n FROM public.news_content_deduplicated').fetchone()['n']==4
        assert target.execute("SELECT canonical_news_id FROM public.news_content_dedup_map WHERE news_id='news-1-alias'").fetchone()['canonical_news_id']=='news-1'
        with pytest.raises(psycopg.Error),target.transaction():
            target.execute('UPDATE news_realtime.catalog SET manifest=manifest')
        body,text,sid=snapshot(cat)
        repo=public.PublicContentRepository(cat,engine)
        with pytest.raises(ValueError):
            asyncio.run(repo.batch_get_by_news_ids(tenant_id='foreign',news_ids=('news-1',)))
        report=asyncio.run(analyze_snapshot(text,sid,TENANTS[0],catalog=cat,content_repository=repo))
        assert len(report['analyses'])==3 and report['external_model_calls']==0
        key=f"{TENANTS[0]}/{body['run_id']}/{START}"
        source.execute('INSERT INTO stream.window_heads(window_key) VALUES(%s)',(key,))
        source.execute('''INSERT INTO stream.snapshots(snapshot_id,window_key,revision,tenant_id,run_id,window_start_ms,window_end_ms,final,payload_text)
            VALUES(%s,%s,1,%s,%s,%s,%s,true,%s)''',(sid,key,TENANTS[0],body['run_id'],START,START+60000,text))
        source.execute("INSERT INTO stream.consumption_ledger(snapshot_id,workflow_id,state) VALUES(%s,%s,'completed')",(sid,'test-'+sid))
        source.execute('INSERT INTO stream.reports(snapshot_id,run_key,workflow_id,trace_id,report) VALUES(%s,%s,%s,%s,%s)',(sid,report['run_key'],'test-'+sid,'0'*32,Jsonb(report)))
        forged=dict(report,tenant_id=TENANTS[1])
        source.execute('UPDATE stream.reports SET report=%s WHERE snapshot_id=%s',(Jsonb(forged),sid))
        with pytest.raises(ValueError,match='Report/catalog'):
            public.project_once(source,target)
        assert target.execute('SELECT count(*) AS n FROM news_realtime.snapshot').fetchone()['n']==0
        source.execute('UPDATE stream.reports SET report=%s WHERE snapshot_id=%s',(Jsonb(report),sid))
        class LostAcknowledgement:
            def execute(self, query, params=None):
                if query.startswith('INSERT INTO stream.warehouse_delivery'):
                    raise RuntimeError('simulated loss after target commit')
                return source.execute(query,params)
        with pytest.raises(RuntimeError): public.project_once(LostAcknowledgement(),target)
        assert target.execute('SELECT count(*) AS n FROM news_realtime.metric').fetchone()['n']==3
        assert public.project_once(source,target)==1
        assert public.project_once(source,target)==0
        joined=target.execute('SELECT * FROM news_realtime.news_detail_current').fetchall()
        assert len(joined)==3 and all(r['body'] and r['source_check_fresh'] for r in joined)
        assert all(r['reader_profile_status']=='unavailable_for_this_minute_window' for r in joined)
        assert {r['news_id']:r['clicks'] for r in joined}=={r['news_id']:r['clicks'] for r in body['metrics']}
        source.execute("INSERT INTO stream.invalid_windows(window_key,reason) VALUES(%s,'test-late-conflict')",(key,))
        public.project_once(source,target)
        assert target.execute('SELECT count(*) AS n FROM news_realtime.news_detail_current').fetchone()['n']==0
        assert target.execute('SELECT count(*) AS n FROM news_realtime.snapshot').fetchone()['n']==1
    engine.dispose()
