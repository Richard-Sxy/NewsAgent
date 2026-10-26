"""Window arithmetic, causal audiences, immutable provenance and SQL publication."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re

import pytest
from app.analytics.entities import ContentType
from app.analytics.news_content import NewsContent, InMemoryNewsContentRepository
from simulation.streaming_lab.bridge import analyze_snapshot, validate_payload, utc
from simulation.streaming_lab.public_news import digest
from simulation.streaming_lab.reader_port import service
from simulation.streaming_lab.window_query import (
    compose, validate_request, WindowUnavailable, WindowBaselineProvider, publish_window, MINUTE,
)
from simulation.streaming_lab.wallclock_data import run_id

TENANT = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
START = int(datetime.now(timezone.utc).timestamp()*1000)//MINUTE*MINUTE-60*MINUTE
TEXT = '公开新闻测试正文，不代表真实用户行为。'


def inputs():
    topics = dict(schema_version=1,taxonomy_version='window-test-v1',status='draft',topics=[
        dict(topic_id=t,name=t,parent_topic_id=None,enabled=True,provenance='draft_extension') for t in ('tech','sport')])
    articles = [dict(news_id=n,content_type='article',record_sha256='a'*64,
        body_sha256=sha256(TEXT.encode()).hexdigest(),publish_ms=START-31*86_400_000) for n in ('n1','n2')]
    manifest = dict(tenants=[TENANT,OTHER],eligible_count=2,rows_sha256=digest(articles),
        content_release_id='test-content',tag_release_id='test-tags')
    catalog = dict(catalog_id='public-'+digest(manifest)[:24],manifest=manifest,
        news={r['news_id']:r for r in articles})
    session = dict(id='window-test',catalog_id=catalog['catalog_id'],start_ms=START,
        manifest=dict(identity_sha256='b'*64,catalog=topics,
            news={r['news_id']:{**r,'topic_ids':['tech' if r['news_id']=='n1' else 'sport']} for r in articles}))
    rows, clicks, visits = [], [], []
    for i in range(30):
        stamp = START+i*MINUTE
        metrics = [dict(news_id='n1',content_type='article',impressions=10 if i<15 else 20,
            clicks=2 if i<15 else 4,unique_users=2,total_duration_seconds=60,
            effective_consumptions=1 if i<15 else 3,interactions=1 if i<15 else 2),
            dict(news_id='n2',content_type='article',impressions=1,clicks=0,unique_users=0,
                 total_duration_seconds=0,effective_consumptions=0,interactions=0)]
        body = dict(metric_version='synthetic-public-news-metrics-v1',catalog_id=catalog['catalog_id'],
            tenant_id=TENANT,run_id=run_id(session,stamp),window_start_ms=stamp,window_end_ms=stamp+MINUTE,
            event_count=30,final=True,event_digest=digest({'minute':i}),metrics=metrics)
        text = json.dumps(body,sort_keys=True,separators=(',',':'))
        rows.append(dict(snapshot_id=sha256(text.encode()).hexdigest(),window_key=f'{TENANT}/{body["run_id"]}/{stamp}',
            window_start_ms=stamp,revision=1,reader_revision=1,invalidated=False,final=True,reader_final=True,
            reader_watermark=stamp+90_000,reader_generated_at=utc(stamp+91_000),reader_event_count=30,payload_text=text))
        for user in ('synthetic-user-0',f'synthetic-user-{1+i%4}'):
            clicks.append(dict(window_ms=stamp,news_id='n1',user_id=user))
            visits.append(dict(tenant_id=TENANT,window_ms=stamp,news_id='n1',user_id=user))
    history = [dict(tenant_id=TENANT,user_id=f'synthetic-user-{i}',news_id='n2',
                    minute_ms=START-60*MINUTE,weighted_score=2) for i in range(5)]
    return dict(session=session,catalog=catalog,tenant=TENANT,start=START+15*MINUTE,minutes=15,
                rows=rows,clicks=clicks,visits=visits,history=history)


def composed(**updates):
    data=inputs();data.update(updates)
    text,sid=compose(**data)
    return data,json.loads(text),text,sid


@pytest.mark.parametrize('minutes',[5,15])
def test_window_counts_exact_union_and_causal_audience(minutes):
    data,body,text,sid=composed(minutes=minutes)
    assert body['metrics'][0]['clicks']==minutes*4
    assert body['metrics'][0]['unique_users']==5  # Not 2 * minutes.
    assert body['reader_snapshot']['items'][0]['reader_count']==5
    assert len(body['source_refs'])==minutes
    assert all(r['profile_cutoff']==utc(data['start']).isoformat().replace('+00:00','Z')
               for r in body['reader_snapshot']['items'])
    assert body['baseline']['window_end_ms']==data['start']
    assert body['baseline']['window_end_ms']-body['baseline']['window_start_ms']==minutes*MINUTE
    assert not re.search(r'synthetic-user-\d+',text) and '"user_id"' not in text
    assert compose(**data)==(text,sid)
    validate_payload(text,sid,TENANT,catalog=data['catalog'])


@pytest.mark.parametrize('kind',['missing','provisional','invalid','reader_pending','no_click_state','reader_count','mixed_catalog'])
def test_incomplete_or_inconsistent_sources_are_rejected(kind):
    data=inputs();row=data['rows'][-1]
    if kind=='missing': data['rows'].pop()
    elif kind=='provisional': row['final']=False
    elif kind=='invalid': row['invalidated']=True
    elif kind=='reader_pending': row['reader_final']=False
    elif kind=='no_click_state': data['clicks']=[]
    elif kind=='reader_count': row['reader_event_count']+=1
    else: data['session']['catalog_id']='public-foreign'
    with pytest.raises(ValueError): compose(**data)


@pytest.mark.parametrize('start,minutes',[(START+1,5),(START,1),(START,60),(START,True)])
def test_unsupported_time_requests_do_not_round(start,minutes):
    with pytest.raises(ValueError):validate_request(TENANT,'window-test',start,minutes)


def test_missing_baseline_is_explicit_and_partial_baseline_is_not_silently_used():
    _,body,_,_=composed(start=START)
    assert body['baseline']['status']=='unavailable_before_session'
    assert body['baseline']['metrics']==[]
    data=inputs();data['rows'].pop(0)
    with pytest.raises(WindowUnavailable):compose(**data)


def test_window_profile_does_not_use_future_history_or_other_tenant_visits():
    data,body,_,_=composed()
    data['history'] += [dict(tenant_id=TENANT,user_id='synthetic-user-0',news_id='n1',
        minute_ms=data['start'],weighted_score=10000)]
    data['visits'] += [dict(tenant_id=OTHER,window_ms=data['start'],news_id='n1',user_id='synthetic-user-99')]
    assert json.loads(compose(**data)[0])['reader_snapshot']==body['reader_snapshot']


def test_source_revision_changes_identity_without_mutating_the_prior_snapshot():
    data,body,text,sid=composed()
    data['rows'][-1]['reader_revision']+=1
    newer,new_id=compose(**data)
    assert new_id!=sid and body['source_refs'][-1]['reader_revision']==1
    assert json.loads(newer)['source_refs'][-1]['reader_revision']==2
    assert sha256(text.encode()).hexdigest()==sid


@pytest.mark.asyncio
async def test_full_existing_hot_news_chain_gets_window_baseline_and_frozen_readers():
    data,body,text,sid=composed()
    readers=service(body['reader_context'],body['reader_snapshot'])
    contents=InMemoryNewsContentRepository([NewsContent(news_id=n,title='测试新闻'+n,summary='科学与体育报道',
        content_type=ContentType.ARTICLE,publish_time=utc(START-31*86_400_000),source_url='https://example.invalid/'+n,body=TEXT)
        for n in ('n1','n2')])
    try:
        report=await analyze_snapshot(text,sid,TENANT,catalog=data['catalog'],content_repository=contents,reader_service=readers)
    finally:
        await readers.close()
    assert len(report['analyses'])==2
    assert report['ranking'][0]['metrics']['unique_users']==5
    assert report['baseline']['metrics'][0]['clicks']==30
    assert report['source_refs']==body['source_refs']
    assert report['reader_profile_status']=='complete'
    assert report['external_model_calls']==0
    # Independently calculated from 60/300,45/60,30/45 and growth 75/135.
    assert abs(Decimal(report['ranking'][0]['hot_score'])-Decimal('0.5391666666666667'))<Decimal('1e-15')
    assert report['event_time_mode']=='wall_clock'
    provider=WindowBaselineProvider(body,'bundle')
    with pytest.raises(ValueError):
        await provider.get_baselines(tenant_id=OTHER,window_start=utc(data['start']),
            window_end=utc(data['start']+15*MINUTE),production_bundle_version='bundle',metric_keys=frozenset())


@pytest.fixture
def database():
    """Opt-in disposable DB only; never initializes an existing application DB."""
    dsn=os.environ.get('WINDOW_QUERY_TEST_DSN')
    if not dsn:pytest.skip('WINDOW_QUERY_TEST_DSN requires an empty disposable database')
    import psycopg
    from psycopg.rows import dict_row
    from simulation.streaming_lab.migrations import migrate
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as db:
        if db.execute("SELECT current_database() AS name").fetchone()['name'].startswith('test_window_') is False:
            pytest.fail('Window integration tests require a test_window_ database')
        assert db.execute("SELECT to_regnamespace('stream') AS ns").fetchone()['ns'] is None
        init=Path(os.environ['WINDOW_QUERY_TEST_INIT_SQL']).read_text()
        # Independent connections exercise the committed MVCC state.
        db.execute(init);migrate(db)
        try:yield db
        finally:db.execute('DROP SCHEMA stream CASCADE')


def seed_database(db,data):
    from psycopg.types.json import Jsonb
    s,c=data['session'],data['catalog']
    db.execute('INSERT INTO stream.public_catalog VALUES(%s,%s)',(c['catalog_id'],Jsonb(c['manifest'])))
    for r in c['news'].values():
        db.execute('INSERT INTO stream.public_news VALUES(%s,%s,%s,%s,%s,%s)',
            (c['catalog_id'],r['news_id'],r['content_type'],r['record_sha256'],r['body_sha256'],r['publish_ms']))
    db.execute('INSERT INTO stream.wallclock_session(id,catalog_id,start_ms,manifest) VALUES(%s,%s,%s,%s)',
        (s['id'],s['catalog_id'],s['start_ms'],Jsonb(s['manifest'])))
    db.execute('INSERT INTO stream.reader_offset(session_id) VALUES(%s)',(s['id'],))
    for r in data['rows']:
        b=json.loads(r['payload_text'])
        db.execute('INSERT INTO stream.window_heads(window_key,revision,event_count,event_digest,final,snapshot_id) VALUES(%s,1,30,%s,true,%s)',
            (r['window_key'],b['event_digest'],r['snapshot_id']))
        db.execute('INSERT INTO stream.snapshots VALUES(%s,%s,1,%s,%s,%s,%s,true,%s,DEFAULT)',
            (r['snapshot_id'],r['window_key'],TENANT,b['run_id'],b['window_start_ms'],b['window_end_ms'],r['payload_text']))
        db.execute('''INSERT INTO stream.reader_window(window_key,session_id,tenant_id,run_id,start_ms,event_count,
            final,revision,watermark_ms,generated_at) VALUES(%s,%s,%s,%s,%s,30,true,1,%s,%s)''',
            (r['window_key'],s['id'],TENANT,b['run_id'],b['window_start_ms'],r['reader_watermark'],r['reader_generated_at']))
    for r in data['clicks']:
        key=f'{TENANT}/{run_id(s,r["window_ms"])}/{r["window_ms"]}'
        db.execute('INSERT INTO stream.reader_click VALUES(%s,%s,%s,%s,%s,%s)',
            (s['id'],key,TENANT,r['window_ms'],r['news_id'],r['user_id']))
    for r in data['visits']:
        key=f'{TENANT}/{run_id(s,r["window_ms"])}/{r["window_ms"]}'
        db.execute('INSERT INTO stream.reader_visit VALUES(%s,%s,%s,%s,%s,%s,0)',
            (s['id'],key,r['tenant_id'],r['window_ms'],r['news_id'],r['user_id']))
    for r in data['history']:
        db.execute('INSERT INTO stream.synthetic_reader_history VALUES(%s,%s,%s,%s,%s,%s)',
            (s['id'],r['tenant_id'],r['user_id'],r['news_id'],r['minute_ms'],r['weighted_score']))


def request(data):
    return dict(tenant_id=TENANT,session_id=data['session']['id'],
        window_start_ms=data['start'],window_minutes=data['minutes'])


def test_sql_publication_replay_and_dependency_invalidation(database):
    data=inputs();seed_database(database,data)
    result=publish_window(database,**request(data))
    repeat=publish_window(database,**request(data))
    assert repeat['snapshot_id']==result['snapshot_id'] and repeat['replayed']
    assert database.execute('SELECT count(*) AS n FROM stream.consumption_ledger').fetchone()['n']==1
    assert database.execute('SELECT count(*) AS n FROM stream.window_dependencies').fetchone()['n']==30
    database.execute("INSERT INTO stream.invalid_windows(window_key,reason) VALUES(%s,'late_conflict')",(data['rows'][0]['window_key'],))
    assert database.execute('SELECT 1 FROM stream.invalid_snapshots WHERE snapshot_id=%s',(result['snapshot_id'],)).fetchone()
    with pytest.raises(WindowUnavailable):publish_window(database,**request(data))


def test_sql_request_cannot_cross_tenant_or_invent_membership(database):
    data=inputs();seed_database(database,data)
    with pytest.raises(WindowUnavailable):publish_window(database,**{**request(data),'tenant_id':OTHER})
    database.execute('DELETE FROM stream.reader_click WHERE window_ms=%s',(data['start'],))
    with pytest.raises(WindowUnavailable):publish_window(database,**request(data))
    assert database.execute('SELECT count(*) AS n FROM stream.consumption_ledger').fetchone()['n']==0


def test_expired_history_is_rejected_before_loading_any_source():
    with pytest.raises(WindowUnavailable,match='reader-history'):
        publish_window(None,tenant_id=TENANT,session_id='window-test',
            window_start_ms=START-2*86_400_000,window_minutes=5)


def test_http_query_enqueues_existing_workflow_and_exposes_saved_scope(database,monkeypatch):
    import psycopg
    from psycopg.rows import dict_row
    from fastapi.testclient import TestClient
    from simulation.streaming_lab import runtime
    data=inputs();seed_database(database,data)
    def connect():return psycopg.connect(os.environ['WINDOW_QUERY_TEST_DSN'],autocommit=True,row_factory=dict_row)
    monkeypatch.setattr(runtime,'connect',connect)
    with TestClient(runtime.app) as client:
        result=client.post('/streaming-lab-api/window-queries',json=request(data))
        assert result.status_code==202,result.text
        saved=result.json()
        response=client.get('/streaming-lab-api/runs/'+saved['run_id'],params={'tenant_id':TENANT})
        assert response.status_code==200,response.text
        snapshot=response.json()['snapshots'][0]
        assert snapshot['workflow_id']=='stream-snapshot-'+saved['snapshot_id']
        assert snapshot['snapshot']['metrics'][0]['unique_users']==5
        assert client.post('/streaming-lab-api/window-queries',json={**request(data),'window_start_ms':data['start']+1}).status_code==422


def test_click_membership_is_deduplicated_and_commits_with_offsets(database):
    from simulation.streaming_lab.realtime_readers import consume
    data=inputs();seed_database(database,data)
    s=data['session'];stamp=START
    event=dict(schema_version=2,synthetic=True,kind='event',catalog_id=s['catalog_id'],tenant_id=TENANT,
        run_id=run_id(s,stamp),event_time_ms=stamp+1000,event_id=f'{run_id(s,stamp)}:{stamp}:click-test',
        news_id='n1',content_type='article',event_type='click',user_id='synthetic-user-99',duration_seconds=0)
    consume(database,s,[(0,event),(1,event)])
    assert database.execute("SELECT count(*) AS n FROM stream.reader_click WHERE user_id='synthetic-user-99'").fetchone()['n']==1
    later={**event,'event_id':event['event_id']+'-next','user_id':'synthetic-user-98'}
    invalid={**event,'event_time_ms':int(datetime.now(timezone.utc).timestamp()*1000)+100000}
    with pytest.raises(ValueError):consume(database,s,[(2,later),(3,invalid)])
    assert database.execute("SELECT 1 FROM stream.reader_click WHERE user_id='synthetic-user-98'").fetchone() is None
    assert database.execute('SELECT next_offset FROM stream.reader_offset').fetchone()['next_offset']==2


def test_concurrent_publishers_reuse_one_snapshot(database):
    from concurrent.futures import ThreadPoolExecutor
    import psycopg
    from psycopg.rows import dict_row
    data=inputs();seed_database(database,data)
    def call():
        with psycopg.connect(os.environ['WINDOW_QUERY_TEST_DSN'],autocommit=True,row_factory=dict_row) as db:
            return publish_window(db,**request(data))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:call(),range(2)))
    assert results[0]['snapshot_id']==results[1]['snapshot_id']
    assert sum(not r['replayed'] for r in results)==1
    assert database.execute('SELECT count(*) AS n FROM stream.consumption_ledger').fetchone()['n']==1


def test_source_change_between_reads_cannot_mix_snapshot_versions(database,monkeypatch):
    import psycopg
    from simulation.streaming_lab import window_query
    data=inputs();seed_database(database,data)
    original=window_query.load_catalog
    def change_after_first_source_read(db,catalog_id):
        result=original(db,catalog_id)
        with psycopg.connect(os.environ['WINDOW_QUERY_TEST_DSN'],autocommit=True) as other:
            row=data['rows'][-1]
            other.execute('INSERT INTO stream.reader_click VALUES(%s,%s,%s,%s,%s,%s)',
                (data['session']['id'],row['window_key'],TENANT,row['window_start_ms'],'n1','synthetic-user-99'))
        return result
    monkeypatch.setattr(window_query,'load_catalog',change_after_first_source_read)
    result=publish_window(database,**request(data))
    payload=database.execute('SELECT payload_text FROM stream.snapshots WHERE snapshot_id=%s',
        (result['snapshot_id'],)).fetchone()['payload_text']
    assert json.loads(payload)['metrics'][0]['unique_users']==5
    monkeypatch.setattr(window_query,'load_catalog',original)
    with pytest.raises(WindowUnavailable):publish_window(database,**request(data))


@pytest.mark.asyncio
@pytest.mark.parametrize('discovery_policy',[None,'hot-discovery-v2'])
async def test_temporal_window_analysis_persists_and_reuses_report(database,monkeypatch,discovery_policy):
    endpoint=os.environ.get('WINDOW_QUERY_TEST_TEMPORAL')
    if not endpoint:pytest.skip('WINDOW_QUERY_TEST_TEMPORAL enables the local workflow round trip')
    from uuid import uuid4
    import psycopg
    from psycopg.rows import dict_row
    from temporalio.client import Client
    from temporalio.worker import Worker
    from simulation.streaming_lab import runtime
    data=inputs();seed_database(database,data)
    result=publish_window(database,**request(data),discovery_policy=discovery_policy)
    def connect():return psycopg.connect(os.environ['WINDOW_QUERY_TEST_DSN'],autocommit=True,row_factory=dict_row)
    contents=InMemoryNewsContentRepository([NewsContent(news_id=n,title='测试新闻'+n,summary='科学与体育报道',
        content_type=ContentType.ARTICLE,publish_time=utc(START-31*86_400_000),source_url='https://example.invalid/'+n,body=TEXT)
        for n in ('n1','n2')])
    class Engine:
        def dispose(self):pass
    monkeypatch.setattr(runtime,'connect',connect)
    monkeypatch.setattr(runtime,'content_engine',Engine)
    monkeypatch.setattr(runtime,'PublicContentRepository',lambda *args:contents)
    monkeypatch.setattr(runtime,'SqlRelatedNewsSearchClient',lambda *args,**kwargs:None)
    client=await Client.connect(endpoint)
    queue='test-window-'+uuid4().hex
    async with Worker(client,task_queue=queue,workflows=[runtime.SnapshotWorkflow],activities=[runtime.analyze_activity]):
        for _ in range(2):
            out=await client.execute_workflow(runtime.SnapshotWorkflow.run,result['snapshot_id'],
                id='test-window-'+uuid4().hex,task_queue=queue)
            assert out['status']=='completed'
    saved=database.execute('SELECT report FROM stream.reports WHERE snapshot_id=%s',(result['snapshot_id'],)).fetchone()['report']
    if discovery_policy:
        assert saved['ranking']==[] and saved['discovery']['qualified_count']==0
        assert saved['discovery']['candidates'][0]['reasons']==['insufficient_unique_users','below_entry_score']
    else:
        assert saved['ranking'][0]['metrics']['unique_users']==5
    assert saved['reader_profile_status']=='complete' and saved['baseline']['status']=='complete'
    assert database.execute('SELECT count(*) AS n FROM stream.reports').fetchone()['n']==1
    assert database.execute('SELECT state FROM stream.consumption_ledger').fetchone()['state']=='completed'


def test_policy_changes_snapshot_and_run_identity_but_not_legacy_arithmetic():
    data,old,_,_=composed()
    text,sid=compose(**data,discovery_policy='hot-discovery-v2')
    new=json.loads(text)
    assert new['run_id']!=old['run_id'] and new['metrics']==old['metrics']
    assert new['discovery_policy']['version']=='hot-discovery-v2'
    validate_payload(text,sid,TENANT,catalog=data['catalog'])
    new['discovery_policy']['entry_score']='0.01'
    text=json.dumps(new)
    with pytest.raises(ValueError):validate_payload(text,sha256(text.encode()).hexdigest(),TENANT,catalog=data['catalog'])


@pytest.mark.asyncio
async def test_discovery_qualified_candidate_reaches_existing_analysis_and_explanation():
    data=inputs()
    for index,row in enumerate(data['clicks']):
        # Keep minute UV=2, but give the full window genuine distinct reach.
        if index%2:row['user_id']=f'synthetic-user-{1+index//2}'
    text,sid=compose(**data,discovery_policy='hot-discovery-v2');body=json.loads(text)
    contents=InMemoryNewsContentRepository([NewsContent(news_id=n,title='测试新闻'+n,summary='科学与体育报道',
        content_type=ContentType.ARTICLE,publish_time=utc(START-31*86_400_000),source_url='https://example.invalid/'+n,body=TEXT)
        for n in ('n1','n2')])
    readers=service(body['reader_context'],body['reader_snapshot'])
    report=await analyze_snapshot(text,sid,TENANT,catalog=data['catalog'],content_repository=contents,reader_service=readers)
    assert len(report['ranking'])==len(report['analyses'])==1
    assert report['ranking'][0]['news_id']=='n1'
    details=report['discovery']['candidates']
    assert details[0]['selected'] and not details[1]['qualifies']
    assert Decimal(details[0]['score'])==sum(Decimal(n) for n in details[0]['components'].values())
    assert report['model_mode']=='deterministic_local_port'


def test_monitor_waits_for_data_and_resumes_without_skipping(database):
    from simulation.streaming_lab.monitor import register,process_one,status
    data=inputs();seed_database(database,data)
    row=register(database,**request(data));mid=row['monitor_id']
    key=data['rows'][20]['window_key']
    database.execute('UPDATE stream.reader_window SET final=false WHERE window_key=%s',(key,))
    assert process_one(database,mid) is False
    state=status(database,TENANT)[0]
    assert state['next_start_ms']==data['start'] and state['status']=='waiting_source'
    assert state['backlog_windows']==1 and state['source_lag_seconds']>0
    assert database.execute('SELECT count(*) AS n FROM stream.consumption_ledger').fetchone()['n']==0
    database.execute('UPDATE stream.reader_window SET final=true WHERE window_key=%s',(key,))
    assert process_one(database,mid)
    assert status(database,TENANT)[0]['next_start_ms']==data['start']+MINUTE
    assert process_one(database,mid) is False  # Next final minute has not arrived.
    assert status(database,TENANT)[0]['next_start_ms']==data['start']+MINUTE


def test_monitor_crash_after_publication_replays_same_snapshot(database,monkeypatch):
    from simulation.streaming_lab import monitor
    data=inputs();seed_database(database,data)
    row=monitor.register(database,**request(data));mid=row['monitor_id']
    original=monitor.publish_window
    def crash(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('Simulated process crash after committed publication')
    monkeypatch.setattr(monitor,'publish_window',crash)
    with pytest.raises(RuntimeError):monitor.process_one(database,mid)
    assert monitor.status(database,TENANT)[0]['next_start_ms']==data['start']
    monkeypatch.setattr(monitor,'publish_window',original)
    assert monitor.process_one(database,mid)
    assert database.execute('SELECT count(*) AS n FROM stream.consumption_ledger').fetchone()['n']==1
    assert database.execute('SELECT count(*) AS n FROM stream.hot_monitor_windows').fetchone()['n']==1


def test_monitor_registration_pause_backpressure_and_cursor_are_persistent(database,monkeypatch):
    from simulation.streaming_lab import monitor
    data=inputs();seed_database(database,data)
    row=monitor.register(database,**request(data));mid=row['monitor_id']
    assert monitor.register(database,**request(data))['monitor_id']==mid
    with pytest.raises(ValueError):monitor.register(database,**{**request(data),'window_start_ms':data['start']+MINUTE})
    monitor.set_enabled(database,mid,False)
    assert monitor.process_one(database,mid) is False
    assert monitor.register(database,**request(data))['enabled'] is False
    monitor.set_enabled(database,mid,True)
    monkeypatch.setattr(monitor,'MAX_PENDING',0)
    assert monitor.process_one(database,mid) is False
    assert monitor.status(database,TENANT)[0]['status']=='backpressure'
    monkeypatch.setattr(monitor,'MAX_PENDING',100)
    assert monitor.process_one(database,mid)
    assert monitor.status(database,OTHER)==[]
    database.execute("UPDATE stream.consumption_ledger SET state='failed',error_type='TestFailure'")
    state=monitor.status(database,TENANT)[0]
    assert state['last_analysis_state']=='failed' and state['failed_windows']==1
    assert state['report_lag_seconds'] is None


def test_concurrent_monitors_do_not_duplicate_one_window(database):
    from concurrent.futures import ThreadPoolExecutor
    import psycopg
    from psycopg.rows import dict_row
    from simulation.streaming_lab.monitor import register,process_one
    data=inputs();seed_database(database,data)
    mid=register(database,**request(data))['monitor_id']
    def call():
        with psycopg.connect(os.environ['WINDOW_QUERY_TEST_DSN'],autocommit=True,row_factory=dict_row) as db:
            return process_one(db,mid)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:call(),range(2)))
    assert sum(results)==1
    assert database.execute('SELECT count(*) AS n FROM stream.hot_monitor_windows').fetchone()['n']==1


def test_monitor_http_registration_pause_and_status(database,monkeypatch):
    from fastapi.testclient import TestClient
    import psycopg
    from psycopg.rows import dict_row
    from simulation.streaming_lab import runtime
    data=inputs();seed_database(database,data)
    def connect():return psycopg.connect(os.environ['WINDOW_QUERY_TEST_DSN'],autocommit=True,row_factory=dict_row)
    monkeypatch.setattr(runtime,'connect',connect)
    with TestClient(runtime.app) as client:
        response=client.post('/streaming-lab-api/hot-monitors',json=request(data))
        assert response.status_code==201,response.text
        mid=response.json()['monitor_id']
        assert client.put('/streaming-lab-api/hot-monitors/'+mid,json={'enabled':False}).json()['status']=='paused'
        assert client.get('/streaming-lab-api/hot-monitors',params={'tenant_id':TENANT}).json()['monitors'][0]['monitor_id']==mid
        assert client.get('/streaming-lab-api/hot-monitors',params={'tenant_id':OTHER}).json()['monitors']==[]
        assert client.post('/streaming-lab-api/hot-monitors',json={**request(data),'window_minutes':1}).status_code==422


def test_monitor_exposes_invalidated_prior_results(database):
    from simulation.streaming_lab.monitor import register,process_one,status
    data=inputs();seed_database(database,data)
    mid=register(database,**request(data))['monitor_id']
    assert process_one(database,mid)
    database.execute('INSERT INTO stream.invalid_windows(window_key,reason) VALUES(%s,%s)',
        (data['rows'][0]['window_key'],'test baseline invalidation'))
    row=status(database,TENANT)[0]
    assert row['last_invalidated'] and row['invalidated_windows']==1


def test_expired_monitor_blocks_without_silently_jumping_cursor(database):
    from simulation.streaming_lab.monitor import register,process_one,status
    data=inputs();seed_database(database,data)
    mid=register(database,**request(data))['monitor_id']
    ancient=START-2*86_400_000
    database.execute('UPDATE stream.hot_monitors SET initial_start_ms=%s,next_start_ms=%s WHERE monitor_id=%s',
        (ancient,ancient,mid))
    assert process_one(database,mid) is False
    row=status(database,TENANT)[0]
    assert row['status']=='blocked' and row['error_type']=='HistoryRetentionExceeded'
    assert row['next_start_ms']==ancient


@pytest.mark.asyncio
async def test_monitor_loop_recovers_connection_and_does_not_hide_programming_errors(monkeypatch):
    from simulation.streaming_lab import monitor
    from simulation.streaming_lab.dispatcher import DispatcherHealth
    attempts=[];delays=[];health=DispatcherHealth()
    def tick(connect):
        attempts.append(1)
        if len(attempts)==1:raise ConnectionError('temporary')
        if len(attempts)==3:raise ValueError('bad schema')
        return False
    async def sleep(seconds):delays.append((seconds,health.state))
    monkeypatch.setattr(monitor,'tick',tick)
    with pytest.raises(ValueError):await monitor.run_monitors(None,health,sleep=sleep)
    assert delays==[(1,'recovering'),(1,'ready')]
    assert health.consecutive_failures==0
