"""Synthetic-only Kafka reader state -> anonymous live/final audience SQL aggregates.

Kafka offsets and dedup/history/visitors commit together. No personal state crosses
into the unified warehouse. Final profiles use the existing causal policy.
"""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import time

from psycopg.types.json import Jsonb
from app.modules.reader_profiles.build import _RollingState, _dimension
from app.modules.reader_profiles.contracts import ReaderProfileSummary, PROFILE_POLICY_VERSION
from app.modules.behavior_tags.build import _event_score
from app.observability.telemetry import configure_telemetry, operation
from simulation.streaming_lab.dynamic_feed import connect
from simulation.streaming_lab.dynamic_data import TENANTS
from simulation.streaming_lab.public_news import connect_target, digest
from simulation.streaming_lab.wallclock_data import TOPIC, HALF_LIFE_MS, fingerprint, run_id


def utc(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc)


def validate_event(e, session, now_ms):
    if (type(e.get('synthetic')) is not bool or e['synthetic'] is not True or e.get('schema_version')!=2
            or e.get('tenant_id') not in TENANTS or e.get('catalog_id')!=session['catalog_id']
            or type(e.get('event_time_ms')) is not int or e['event_time_ms']>now_ms+1000):
        raise ValueError('Invalid synthetic wall-clock envelope')
    start=e['event_time_ms']//60000*60000
    if type(e.get('source_sequence',0)) is not int or e.get('source_sequence',0)<0:
        raise ValueError('Invalid synthetic source position')
    if start<session['start_ms'] or e.get('run_id')!=run_id(session,start):
        raise ValueError('Wall-clock session/window identity mismatch')
    if e.get('kind')=='watermark':
        return start
    news=session['manifest']['news'].get(e.get('news_id'))
    if (not news or news['content_type']!=e.get('content_type') or news['publish_ms']>start
            or e.get('user_id') not in {f'synthetic-user-{i}' for i in range(100)}
            or not e.get('event_id','').startswith(f'{e["run_id"]}:{start}:')
            or len(e['event_id'])>160 or e.get('kind')!='event'
            or e.get('event_type') not in ('impression','click','detail_view','read','play','like','comment','share','favorite')
            or type(e.get('duration_seconds')) is not int or not 0<=e['duration_seconds']<=3600
            or (e['event_type']=='read' and news['content_type']!='article')
            or (e['event_type']=='play' and news['content_type']!='video')):
        raise ValueError('Invalid synthetic reader event')
    return start


def event_score(e):
    kind=e['event_type']
    if kind in ('read','play'):
        return _event_score(dict(event_type='detail_view',duration_seconds=e['duration_seconds'],
            effective_read=e['duration_seconds']>=(20 if e['content_type']=='article' else 10)))
    if kind in ('like','comment','share','favorite'):
        return _event_score(dict(event_type='interaction'))
    return _event_score(dict(event_type=kind,duration_seconds=0,effective_read=False))


def consume(db, session, records):
    """Caller passes Kafka partition-zero records. Replay is safe after any crash."""
    with db.transaction():
        cursor=db.execute('SELECT * FROM stream.reader_offset WHERE session_id=%s FOR UPDATE',(session['id'],)).fetchone()
        watermark=cursor['watermark_ms']
        next_offset=cursor['next_offset']
        for offset,e in records:
            if offset<next_offset:
                continue
            if offset!=next_offset:
                raise ValueError('Kafka retention gap; offset recovery requires explicit new session')
            start=validate_event(e,session,int(time.time()*1000))
            next_offset=offset+1
            if e['kind']=='watermark':
                watermark=max(watermark,e['event_time_ms']-2001)
                continue
            key=f'{e["tenant_id"]}/{e["run_id"]}/{start}'
            if watermark>=start+90000:
                db.execute("INSERT INTO stream.rejections(window_key,event_id,reason) VALUES(%s,%s,'too_late') ON CONFLICT DO NOTHING",(key,e['event_id']))
                continue
            signature=fingerprint(e)
            old=db.execute('SELECT fingerprint FROM stream.reader_seen WHERE session_id=%s AND tenant_id=%s AND event_id=%s',
                           (session['id'],e['tenant_id'],e['event_id'])).fetchone()
            if old:
                if old['fingerprint']!=signature:
                    db.execute("INSERT INTO stream.invalid_windows(window_key,reason) VALUES(%s,'reader_conflicting_event_id') ON CONFLICT DO NOTHING",(key,))
                continue
            db.execute('INSERT INTO stream.reader_seen VALUES(%s,%s,%s,%s,%s)',(session['id'],e['tenant_id'],e['event_id'],start,signature))
            db.execute('''INSERT INTO stream.reader_window(window_key,session_id,tenant_id,run_id,start_ms,event_count,last_event_ms)
                VALUES(%s,%s,%s,%s,%s,1,%s) ON CONFLICT(window_key) DO UPDATE SET
                event_count=stream.reader_window.event_count+1,last_event_ms=greatest(stream.reader_window.last_event_ms,EXCLUDED.last_event_ms),updated_at=clock_timestamp()''',
                (key,session['id'],e['tenant_id'],e['run_id'],start,e['event_time_ms']))
            if e['event_type']=='detail_view':
                db.execute('INSERT INTO stream.reader_visit VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                           (session['id'],key,e['tenant_id'],start,e['news_id'],e['user_id'],e.get('source_sequence',0)))
            if e['event_type']=='click':
                db.execute('INSERT INTO stream.reader_click VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                           (session['id'],key,e['tenant_id'],start,e['news_id'],e['user_id']))
            score=event_score(e)
            if score:
                # Bucket coefficients preserve event-level exponential decay exactly.
                weighted=score*2**((e['event_time_ms']-start)/HALF_LIFE_MS)
                db.execute('''INSERT INTO stream.synthetic_reader_history VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(session_id,tenant_id,user_id,news_id,minute_ms) DO UPDATE SET
                    weighted_score=stream.synthetic_reader_history.weighted_score+EXCLUDED.weighted_score''',
                    (session['id'],e['tenant_id'],e['user_id'],e['news_id'],start,weighted))
        db.execute('UPDATE stream.reader_offset SET next_offset=%s,watermark_ms=%s,updated_at=clock_timestamp() WHERE session_id=%s',
                   (next_offset,watermark,session['id']))


def summaries(session, tenant, start, news_ids, visits, history):
    """Same policy as offline reader_profiles.build, with exact prewindow filtering."""
    catalog=session['manifest']['news']
    known={f'synthetic-user-{i}':i<90 for i in range(100)}
    events=sorted([(utc(r['minute_ms']),r['user_id'],r['news_id'],'detail_view',
        tuple(catalog[r['news_id']]['topic_ids']),r['weighted_score']) for r in history
        if r['tenant_id']==tenant and start-720*3600000<=r['minute_ms']<start],key=lambda e:e[0])
    states=[]
    for hours in (720,24):
        state=_RollingState(events,hours,utc(start))
        state.advance(utc(start))
        profiles={u:state.profile(u,available) for u,available in known.items()}
        base=(Counter(t for topics,_,_ in profiles.values() for t in topics),sum(p[1] for p in profiles.values()),profiles)
        states.append((state,base))
    result=[]
    for news in sorted(news_ids):
        visitors={r['user_id'] for r in visits if r['news_id']==news}
        dimensions=[_dimension(state,set(known),visitors,known,base,news,top_k=20,minimum_sample=5,names={}) for state,base in states]
        item=ReaderProfileSummary(news_id=news,source_mode='synthetic',
            taxonomy_version=session['manifest']['catalog']['taxonomy_version'],profile_policy_version=PROFILE_POLICY_VERSION,
            content_sha256=catalog[news]['body_sha256'],content_topic_ids=tuple(catalog[news]['topic_ids']),
            reader_count=len(visitors),long_term=dimensions[0],short_term=dimensions[1],profile_cutoff=utc(start),
            trigger_article_excluded=True,count_method='exact_synthetic_distinct',status='complete' if visitors else 'empty',
            limitations=('Warm-start history is deterministic synthetic seed data, followed by live synthetic sessions.',
                'Content labels are inferred; audience labels are historical reader interests, not the current article labels.',
                'Topics overlap; omitted top-20 topics are unknown, not zero. Do not add UV across windows.'))
        result.append(item.model_dump(mode='json'))
    return result


def materialize(db, target, session):
    cursor=db.execute('SELECT * FROM stream.reader_offset WHERE session_id=%s',(session['id'],)).fetchone()
    watermark=cursor['watermark_ms']
    # Propagate outages/conflicts before publishing a potentially affected projection.
    db.execute('''INSERT INTO stream.invalid_windows(window_key,reason)
        SELECT w.window_key,'wallclock_coverage_gap' FROM stream.reader_window w
        JOIN stream.wallclock_gap g ON g.session_id=w.session_id
        AND g.start_ms<w.start_ms+80000 AND g.end_ms>w.start_ms
        WHERE w.session_id=%s ON CONFLICT DO NOTHING''',(session['id'],))
    invalid=db.execute('SELECT window_key,reason,observed_at FROM stream.invalid_windows').fetchall()
    with target.transaction():
        for r in invalid:
            target.execute('INSERT INTO news_realtime.invalid_window VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',tuple(r.values()))
    windows=db.execute('''SELECT w.*,p.payload,p.updated_at AS metric_updated_at FROM stream.reader_window w
        JOIN stream.metric_preview p USING(window_key) WHERE w.session_id=%s
        AND NOT EXISTS(SELECT 1 FROM stream.invalid_windows i WHERE i.window_key=w.window_key)
        AND (NOT w.final OR NOT w.delivered OR w.start_ms>%s) ORDER BY w.start_ms,w.tenant_id''',(session['id'],int(time.time()*1000)-300000)).fetchall()
    for w in windows:
        body=w['payload']
        metric_updated_at=w['metric_updated_at']
        if not body['final'] and not body.get('source_sequence'):
            continue  # Historical preview without a position cannot provide aligned live interests.
        if w['final']:
            # Source-final commit can precede target commit; this is also the retry path.
            items=w['items']
        else:
            history=db.execute('''SELECT tenant_id,user_id,news_id,minute_ms,weighted_score FROM stream.synthetic_reader_history
                WHERE session_id=%s AND tenant_id=%s AND minute_ms>=%s AND minute_ms<%s''',
                (session['id'],w['tenant_id'],w['start_ms']-720*3600000,w['start_ms'])).fetchall()
            visits=db.execute('''SELECT news_id,user_id FROM stream.reader_visit WHERE window_key=%s
                AND (%s OR first_sequence<=%s)''',
                (w['window_key'],body['final'],body.get('source_sequence',0))).fetchall()
            items=summaries(session,w['tenant_id'],w['start_ms'],[r['news_id'] for r in body['metrics']],visits,history)
            final=body['final'] and watermark>=w['start_ms']+90000
            if final and body['event_count']!=w['event_count']:
                raise ValueError('Flink/reader distinct-event reconciliation failed')
            if final:
                seen=db.execute('''SELECT event_id,fingerprint FROM stream.reader_seen
                    WHERE session_id=%s AND tenant_id=%s AND window_ms=%s ORDER BY event_id COLLATE "C" ''',
                    (session['id'],w['tenant_id'],w['start_ms'])).fetchall()
                event_digest=sha256(json.dumps({r['event_id']:r['fingerprint'] for r in seen},separators=(',',':')).encode()).hexdigest()
                if body['event_digest']!=event_digest:
                    raise ValueError('Flink/reader semantic event digest reconciliation failed')
            w=db.execute('''UPDATE stream.reader_window SET items=%s,final=%s,revision=revision+1,
                watermark_ms=%s,generated_at=clock_timestamp() WHERE window_key=%s RETURNING *''',
                (Jsonb(items),final,watermark,w['window_key'])).fetchone()
        with operation('streaming.readers.project',attributes={'newsagent.run_id':w['run_id']}), target.transaction():
            target.execute('''INSERT INTO news_realtime.live_window(window_key,session_id,catalog_id,tenant_id,run_id,start_ms,
                metrics,readers,revision,final,watermark_ms,event_count,last_event_ms,source_updated_at,source_sequence)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(window_key) DO UPDATE SET metrics=EXCLUDED.metrics,readers=EXCLUDED.readers,
                revision=EXCLUDED.revision,final=EXCLUDED.final,watermark_ms=EXCLUDED.watermark_ms,
                event_count=EXCLUDED.event_count,last_event_ms=EXCLUDED.last_event_ms,source_updated_at=EXCLUDED.source_updated_at,source_sequence=EXCLUDED.source_sequence,
                projected_at=clock_timestamp(),checked_at=clock_timestamp()
                WHERE news_realtime.live_window.revision<EXCLUDED.revision''',
                (w['window_key'],session['id'],session['catalog_id'],w['tenant_id'],w['run_id'],w['start_ms'],
                 Jsonb(body['metrics']),Jsonb(items),w['revision'],w['final'],w['watermark_ms'],body['event_count'],
                 body.get('max_event_time_ms') or w['last_event_ms'],min(w['generated_at'],metric_updated_at),body.get('source_sequence',0)))
            if w['final']:
                identity=digest(dict(window_key=w['window_key'],items=items,revision=w['revision'],watermark_ms=w['watermark_ms']))
                old=target.execute('SELECT identity_sha256 FROM news_realtime.reader_final WHERE window_key=%s',(w['window_key'],)).fetchone()
                if old and old['identity_sha256']!=identity:
                    raise ValueError('Immutable reader final collision')
                target.execute('''INSERT INTO news_realtime.reader_final VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING''',(w['window_key'],session['id'],session['manifest']['identity_sha256'],w['tenant_id'],
                        w['run_id'],w['start_ms'],w['revision'],w['watermark_ms'],w['generated_at'],Jsonb(items),identity))
        if w['final']:
            db.execute('UPDATE stream.reader_window SET delivered=true WHERE window_key=%s',(w['window_key'],))
    with target.transaction():
        target.execute('UPDATE news_realtime.live_window SET checked_at=%s WHERE session_id=%s AND start_ms>%s',
            (cursor['updated_at'],session['id'],int(time.time()*1000)-300000))


def worker():
    from kafka import KafkaConsumer, TopicPartition
    telemetry=configure_telemetry()
    consumer=KafkaConsumer(bootstrap_servers='kafka:9092',enable_auto_commit=False,auto_offset_reset='none',
        max_poll_records=1000,request_timeout_ms=15000)
    partition=TopicPartition(TOPIC,0)
    try:
        with connect() as db, connect_target() as target:
            from simulation.streaming_lab.migrations import migrate
            migrate(db)
            if not db.execute('SELECT pg_try_advisory_lock(480009) AS ok').fetchone()['ok']:
                raise ValueError('Reader consumer already active')
            session=db.execute('SELECT * FROM stream.wallclock_session ORDER BY start_ms DESC LIMIT 1').fetchone()
            offset=db.execute('SELECT next_offset FROM stream.reader_offset WHERE session_id=%s',(session['id'],)).fetchone()['next_offset']
            consumer.assign([partition]); consumer.seek(partition,offset)
            projected=0
            while True:
                batches=consumer.poll(timeout_ms=500)
                rows=[(r.offset,json.loads(r.value)) for r in batches.get(partition,())]
                if rows:
                    consume(db,session,rows)
                if time.monotonic()-projected>=3:
                    materialize(db,target,session)
                    projected=time.monotonic()
                    db.execute('DELETE FROM stream.reader_seen WHERE session_id=%s AND window_ms<%s',(session['id'],int(time.time()*1000)-600000))
                    db.execute('DELETE FROM stream.synthetic_reader_history WHERE session_id=%s AND minute_ms<%s',(session['id'],int(time.time()*1000)-31*86400000))
    finally:
        consumer.close()
        telemetry.shutdown()


if __name__=='__main__':
    worker()
