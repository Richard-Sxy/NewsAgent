"""Wall-clock causality, deterministic retries and exact interest membership."""
from copy import deepcopy
from datetime import timedelta
import time
import asyncio
import os

import pytest
from simulation.streaming_lab.wallclock_data import packet,due,TENANTS,seed_history,run_id
from simulation.streaming_lab.realtime_readers import summaries,utc,validate_event
from simulation.streaming_lab.reader_port import service
from app.modules.reader_profiles.contracts import ReaderProfileQuery

START=int(time.time()*1000)//60000*60000-600000


def session():
    catalog=dict(schema_version=1,taxonomy_version='test-taxonomy-v1',status='draft',topics=[
        dict(topic_id=t,name=t,parent_topic_id=None,enabled=True,provenance='draft_extension') for t in ('tech','sport')])
    news={n:dict(news_id=n,topic_ids=[topic],content_type='article',publish_ms=START-31*86400000,
        body_sha256='a'*64,record_sha256='b'*64) for n,topic in [('n1','tech'),('n2','sport'),('n3','tech'),('n4','sport')]}
    return dict(id='abc123',catalog_id='public-'+'a'*24,start_ms=START,
        manifest=dict(news=news,roots=['sport','tech'],catalog=catalog,identity_sha256='c'*64))


def test_real_clock_packet_is_replayable_has_overlap_late_duplicates_and_shared_users():
    s=session();p=packet(s,START)
    assert p==packet(s,START)
    assert len(p)>len({e['event_id'] for _,e in p})
    assert any(delivery>START+60000 and e['event_time_ms']<START+60000 for delivery,e in p)
    assert all(e['event_time_ms']<=delivery for delivery,e in p)
    assert {e['news_id'] for _,e in p}=={e['news_id'] for _,e in packet(s,START+60000)}
    seen={}
    for _,e in p:
        if e['event_type']=='detail_view':
            seen.setdefault((e['tenant_id'],e['user_id']),set()).add(e['news_id'])
    assert any(len(news)>1 for news in seen.values())
    scheduled=[e for second in range(START//1000,START//1000+80) for e in due(s,second,{}) if e['kind']=='event' and e['run_id']==run_id(s,START)]
    assert len(scheduled)==len(p)
    assert all(e['event_time_ms']<=second*1000 for second in range(START//1000,START//1000+60)
               for e in due(s,second,{}) if e['kind']=='watermark')


def test_interest_uses_distinct_detail_readers_prewindow_history_excludes_trigger_and_tenant():
    s=session();tenant=TENANTS[0]
    def hist(user,news,stamp=START-3600000,scope=tenant):
        return dict(tenant_id=scope,user_id=user,news_id=news,minute_ms=stamp,weighted_score=2)
    visitors=[dict(news_id='n1',user_id=f'synthetic-user-{i}') for i in (0,0,1,80,95)]
    history=[hist('synthetic-user-0','n2'),hist('synthetic-user-0','n1'),hist('synthetic-user-1','n1'),
        hist('synthetic-user-80','n3',START),hist('synthetic-user-1','n3',scope=TENANTS[1])]
    item=summaries(s,tenant,START,['n1'],visitors,history)[0]
    assert item['reader_count']==4
    for dim in ('long_term','short_term'):
        value=item[dim]
        assert value['profiled_reader_count']==1
        assert value['unknown_reader_count']==3
        assert value['cold_start_reader_count']==2
        assert [t['topic_id'] for t in value['topics']]==['sport']
        assert value['topics'][0]['lift'] is None
    assert item['profile_cutoff']==utc(START).isoformat().replace('+00:00','Z')


def test_seed_respects_publication_and_short_long_are_separate():
    s=session()
    rows=list(seed_history(s))
    assert rows and all(s['manifest']['news'][r[3]]['publish_ms']<=r[4]<START for r in rows)
    history=[dict(tenant_id=TENANTS[0],user_id='synthetic-user-0',news_id='n2',minute_ms=START-25*3600000,weighted_score=2)]
    item=summaries(s,TENANTS[0],START,['n1'],[dict(news_id='n1',user_id='synthetic-user-0')],history)[0]
    assert item['long_term']['profiled_reader_count']==1
    assert item['short_term']['profiled_reader_count']==0


@pytest.mark.parametrize('change',['future','tenant','catalog','news','user','synthetic','run'])
def test_event_identity_fail_closed(change):
    s=session();e=deepcopy(packet(s,START)[0][1])
    key,value={'future':('event_time_ms',START+10000000),'tenant':('tenant_id','other'),
        'catalog':('catalog_id','other'),'news':('news_id','other'),'user':('user_id','real-user'),
        'synthetic':('synthetic',False),'run':('run_id','other')}[change]
    e[key]=value
    with pytest.raises(ValueError):validate_event(e,s,START+100000)


def test_final_port_passes_existing_strict_service_and_rejects_foreign_window():
    s=session();items=summaries(s,TENANTS[0],START,['n1'],[],[])
    row=dict(tenant_id=TENANTS[0],start_ms=START,items=items,identity_sha256='d'*64,revision=1,
        generated_at=utc(START+95000),watermark_ms=START+92000)
    client=service(s,row)
    query=ReaderProfileQuery(tenant_id=TENANTS[0],news_ids=('n1',),window_start=utc(START),
        window_end=utc(START+60000),source_release_sha256='c'*64)
    async def check():
        assert (await client.fetch(query)).items[0].reader_count==0
        with pytest.raises(Exception):
            await client.fetch(query.model_copy(update={'window_end':utc(START+120000)}))
        await client.close()
    asyncio.run(check())


def test_postgres_offsets_dedup_conflicts_rollback_and_too_late():
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from uuid import uuid4
    from urllib.parse import urlparse
    from simulation.streaming_lab.realtime_readers import consume
    dsn=os.environ.get('PUBLIC_NEWS_TEST_DSN')
    if not dsn:
        pytest.skip('Explicit isolated PostgreSQL test database required')
    assert urlparse(dsn).path.startswith('/newsagent_sql_test_')
    s=session();s['id']=uuid4().hex[:12]
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as db:
        db.execute('INSERT INTO stream.public_catalog(catalog_id,manifest) VALUES(%s,%s) ON CONFLICT DO NOTHING',(s['catalog_id'],Jsonb({})))
        db.execute('INSERT INTO stream.wallclock_session(id,catalog_id,start_ms,manifest) VALUES(%s,%s,%s,%s)',(s['id'],s['catalog_id'],START,Jsonb(s['manifest'])))
        db.execute('INSERT INTO stream.reader_offset(session_id) VALUES(%s)',(s['id'],))
        events=[e for _,e in packet(s,START) if e['event_type']=='detail_view']
        e=events[0]
        consume(db,s,[(0,e),(1,e)])
        assert db.execute('SELECT sum(event_count) AS n FROM stream.reader_window WHERE session_id=%s',(s['id'],)).fetchone()['n']==1
        assert db.execute('SELECT count(*) AS n FROM stream.reader_visit WHERE session_id=%s',(s['id'],)).fetchone()['n']==1
        future={**events[1],'event_time_ms':int(time.time()*1000)+100000}
        with pytest.raises(ValueError):consume(db,s,[(2,events[1]),(3,future)])
        assert db.execute('SELECT next_offset FROM stream.reader_offset WHERE session_id=%s',(s['id'],)).fetchone()['next_offset']==2
        assert db.execute('SELECT sum(event_count) AS n FROM stream.reader_window WHERE session_id=%s',(s['id'],)).fetchone()['n']==1
        conflict={**e,'user_id':'synthetic-user-99' if e['user_id']!='synthetic-user-99' else 'synthetic-user-98'}
        consume(db,s,[(2,conflict)])
        assert db.execute('SELECT count(*) AS n FROM stream.invalid_windows WHERE window_key LIKE %s',('%'+s['id']+'%',)).fetchone()['n']==1
        marker=dict(schema_version=2,synthetic=True,kind='watermark',catalog_id=s['catalog_id'],tenant_id=TENANTS[0],
            run_id=run_id(s,START+60000),event_time_ms=START+94000)
        consume(db,s,[(3,marker),(4,events[1])])
        assert db.execute("SELECT count(*) AS n FROM stream.rejections WHERE window_key LIKE %s AND reason='too_late'",('%'+s['id']+'%',)).fetchone()['n']==1
