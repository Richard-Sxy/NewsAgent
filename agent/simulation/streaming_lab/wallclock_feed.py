"""SQL-checkpointed wall-clock publisher; bounded retries, explicit pause on failure."""
import argparse
from datetime import datetime, timezone
import json
import os
import time

from psycopg.types.json import Jsonb
from simulation.streaming_lab.dynamic_feed import connect
from simulation.streaming_lab.public_news import connect_target, initialize_target, digest, load_catalog
from simulation.streaming_lab.migrations import migrate
from simulation.streaming_lab.wallclock_data import VERSION, TOPIC, due, seed_history
from app.observability.telemetry import configure_telemetry, operation


def prepare():
    with connect() as db, connect_target() as target:
        migrate(db)
        initialize_target(target)
        active = db.execute('SELECT * FROM stream.wallclock_session ORDER BY start_ms DESC LIMIT 1').fetchone()
        if active:
            return active
        feed = db.execute('SELECT * FROM stream.dynamic_feed WHERE id=1').fetchone()
        if feed['enabled']:
            raise ValueError('Pause accelerated feed before wall-clock migration')
        catalog = load_catalog(target,feed['public_catalog_id'],target=True)
        m = catalog['manifest']
        policy = target.execute('SELECT policy FROM public.news_content_tag_releases WHERE tenant_id=%s AND tag_release_id=%s',
            (m['content_tenant_id'],m['tag_release_id'])).fetchone()['policy']
        tags = target.execute('SELECT news_id,topic_ids FROM public.news_content_tags WHERE tenant_id=%s AND tag_release_id=%s',
            (m['content_tenant_id'],m['tag_release_id'])).fetchall()
        lookup = {r['news_id']:r['topic_ids'] for r in tags}
        news = {n:{**r,'news_id':n,'topic_ids':lookup[n]} for n,r in catalog['news'].items()}
        start = (int(time.time()*1000)//60000+1)*60000
        manifest = dict(version=VERSION,topic=TOPIC,news=news,catalog=policy['catalog'],
            roots=sorted(t['topic_id'] for t in policy['catalog']['topics'] if not t.get('parent_topic_id')),
            warm_start='deterministic-synthetic-30-day-history-v1',max_hours=24,
            history_source='synthetic_seed_and_live_sessions',first_complete_window_ms=start)
        manifest['identity_sha256'] = digest(dict(manifest=manifest,start_ms=start,catalog_id=catalog['catalog_id']))
        session = dict(id=manifest['identity_sha256'][:12],catalog_id=catalog['catalog_id'],start_ms=start,manifest=manifest)
        with db.transaction():
            db.execute('INSERT INTO stream.wallclock_session(id,catalog_id,start_ms,manifest) VALUES(%s,%s,%s,%s)',
                (session['id'],session['catalog_id'],start,Jsonb(manifest)))
            db.execute('INSERT INTO stream.reader_offset(session_id) VALUES(%s)',(session['id'],))
            with db.cursor() as cursor:
                cursor.executemany('INSERT INTO stream.synthetic_reader_history VALUES(%s,%s,%s,%s,%s,%s)',seed_history(session))
        return session


def worker():
    from kafka import KafkaProducer
    telemetry = configure_telemetry()
    producer = KafkaProducer(bootstrap_servers='kafka:9092',acks='all',enable_idempotence=True,
        retries=1,max_in_flight_requests_per_connection=1,request_timeout_ms=8000,
        delivery_timeout_ms=15000,max_block_ms=5000,linger_ms=5)
    packets = {}
    cached = None
    try:
        with connect() as db:
            if not db.execute('SELECT pg_try_advisory_lock(480008) AS ok').fetchone()['ok']:
                raise RuntimeError('Wall-clock publisher already active')
            while True:
                status = db.execute('SELECT id,enabled,last_second FROM stream.wallclock_session ORDER BY start_ms DESC LIMIT 1').fetchone()
                if status and (not cached or cached['id']!=status['id']):
                    cached = db.execute('SELECT * FROM stream.wallclock_session WHERE id=%s',(status['id'],)).fetchone()
                session = {**cached,**status} if status else None
                # Publish a completed second. Events within that second may have
                # millisecond timestamps; opening the second is too early for them.
                now = int(time.time())-1
                if not session or not session['enabled'] or now*1000<session['start_ms']:
                    time.sleep(.5)
                    continue
                try:
                    if now*1000>=session['start_ms']+session['manifest']['max_hours']*3600000:
                        raise ValueError('WallClock24HourBudgetReached')
                    if db.execute('SELECT pg_database_size(current_database()) AS size').fetchone()['size']>768*1024**2:
                        raise ValueError('WallClockDatabaseBudgetReached')
                    second = session['last_second']+1 if session['last_second'] is not None else session['start_ms']//1000
                    if second>now:
                        time.sleep(.15)
                        continue
                    if now-second>15:
                        # A real outage is not silently backfilled as if it were live.
                        with db.transaction():
                            db.execute('INSERT INTO stream.wallclock_gap VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                                       (session['id'],second*1000,now*1000))
                            db.execute('UPDATE stream.wallclock_session SET last_second=%s WHERE id=%s',(now-1,session['id']))
                        second = now
                    attempts = db.execute('UPDATE stream.wallclock_session SET attempts=attempts+1 WHERE id=%s RETURNING attempts',
                        (session['id'],)).fetchone()['attempts']
                    if attempts>2:
                        raise ValueError('WallClockPublicationAttemptsExhausted')
                    with operation('streaming.wallclock.publish',attributes={'newsagent.run_id':session['id']}):
                        for index, original in enumerate(due(session,second,packets)):
                            event={**original,'source_sequence':second*10000+index}
                            if event['event_time_ms']>int(time.time()*1000):
                                raise ValueError('FutureSyntheticEvent')
                            producer.send(TOPIC,partition=0,value=json.dumps(event,separators=(',',':')).encode()).get(timeout=15)
                        db.execute('UPDATE stream.wallclock_session SET last_second=%s,attempts=0,last_error=NULL,updated_at=clock_timestamp() WHERE id=%s',
                                   (second,session['id']))
                except Exception as exc:
                    db.execute('UPDATE stream.wallclock_session SET enabled=false,last_error=%s,updated_at=clock_timestamp() WHERE id=%s',
                               (type(exc).__name__+':'+str(exc)[:160],session['id']))
                    print(json.dumps({'state':'paused','error_type':type(exc).__name__}),flush=True)
                    time.sleep(1)
    finally:
        producer.close(timeout=2)
        telemetry.shutdown()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','worker','pause','resume','status'])
    args=parser.parse_args()
    if args.command=='worker':
        worker()
        return
    if args.command=='prepare':
        s=prepare()
        print(json.dumps({k:s[k] for k in ('id','catalog_id','start_ms')}))
        return
    with connect() as db:
        if args.command in ('pause','resume'):
            if args.command=='resume' and db.execute('SELECT enabled FROM stream.dynamic_feed WHERE id=1').fetchone()['enabled']:
                raise ValueError('Accelerated feed must remain paused')
            db.execute('UPDATE stream.wallclock_session SET enabled=%s,last_error=NULL WHERE id=(SELECT id FROM stream.wallclock_session ORDER BY start_ms DESC LIMIT 1)',(args.command=='resume',))
        print(json.dumps(db.execute('SELECT id,start_ms,enabled,last_second,last_error,updated_at FROM stream.wallclock_session ORDER BY start_ms DESC LIMIT 1').fetchone(),default=str))


if __name__=='__main__':
    main()
