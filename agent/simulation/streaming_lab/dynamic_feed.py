"""Temporal-paced local traffic with SQL progress, bounded retries and exact recount.

Only the dedicated lab worker imports Kafka. No production bootstrap depends on it.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import time
from urllib.request import build_opener, ProxyHandler

import psycopg
from psycopg.rows import dict_row
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker

from app.observability.telemetry import configure_telemetry, operation
from simulation.streaming_lab.dynamic_data import VERSION, TENANTS, packet, digest, encode, enforce_budget

QUEUE = 'newsagent-dynamic-feed-v1'
WORKFLOW_ID = 'newsagent-dynamic-feed-v1'
OWNER = 'dynamic-feed-v1'
STATE = Path(os.environ.get('LAB_PRODUCER_STATE', '/producer-state'))
HTTP = build_opener(ProxyHandler({}))


def connect():
    return psycopg.connect(os.environ['LAB_DSN'], autocommit=True, row_factory=dict_row,
                          connect_timeout=5, options='-c statement_timeout=10000')


def read_json(url):
    with HTTP.open(url, timeout=4) as response:
        return json.load(response)


def ensure_lock():
    path = STATE / 'producer.lock'
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(OWNER)
    except FileExistsError:
        if path.read_text(encoding='utf-8') != OWNER:
            raise ValueError('AnotherSyntheticProducerOwnsClock')


def release_lock():
    path = STATE / 'producer.lock'
    if path.exists() and path.read_text(encoding='utf-8') == OWNER:
        path.unlink()  # This one ephemeral owner file only; no directory/data deletion.


def plan(db, activity_key):
    """Caller holds the session advisory lock through publication and verification."""
    with db.transaction():
        feed = db.execute('SELECT * FROM stream.dynamic_feed WHERE id=1 FOR UPDATE').fetchone()
        replay = db.execute('SELECT * FROM stream.dynamic_batches WHERE activity_key=%s',(activity_key,)).fetchone()
        if replay:
            return replay, feed['interval_seconds']
        unfinished = db.execute("SELECT * FROM stream.dynamic_batches WHERE state<>'completed' ORDER BY sequence LIMIT 1").fetchone()
        if not feed['enabled']:
            # An uncertain in-flight batch retains the clock lock until diagnosed/resumed.
            if not unfinished:
                release_lock()
            return None, feed['interval_seconds']
        ensure_lock()
        if unfinished:
            # A manual resume after an exhausted Activity takes ownership of the
            # same unfinished batch. Its retries retain this new Activity identity.
            unfinished = db.execute('UPDATE stream.dynamic_batches SET activity_key=%s WHERE sequence=%s RETURNING *',
                                    (activity_key,unfinished['sequence'])).fetchone()
            return unfinished, feed['interval_seconds']
        if feed['sequence'] >= feed['max_windows']:
            db.execute("UPDATE stream.dynamic_feed SET enabled=false,last_error='WindowBudgetReached',updated_at=clock_timestamp() WHERE id=1")
            release_lock()
            return None, feed['interval_seconds']
        clock_path = STATE / 'event-clock.json'
        external_clock = json.loads(clock_path.read_text(encoding='utf-8'))['last_start_ms'] if clock_path.exists() else 0
        previous = db.execute('SELECT coalesce(max(window_start_ms),0) AS stamp FROM stream.snapshots').fetchone()['stamp']
        last_batch = db.execute('SELECT coalesce(max(window_start_ms),0) AS stamp FROM stream.dynamic_batches').fetchone()['stamp']
        start = max(int(time.time()*1000)//60000*60000, max(external_clock, previous, last_batch)+180000)
        sequence = feed['sequence']+1
        catalog_id=feed.get('public_catalog_id')
        if catalog_id:
            from simulation.streaming_lab.public_news import load_catalog, public_packet
            value=public_packet(sequence,start,load_catalog(db,catalog_id))
        else:
            value = packet(sequence, start)
        row = db.execute('''INSERT INTO stream.dynamic_batches(sequence,run_id,window_start_ms,generator_version,
            scenario,packet_sha256,event_count,activity_key,public_catalog_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (sequence,value['run_id'],start,value['version'],value['scenario'],digest(value),len(value['events']),activity_key,catalog_id)).fetchone()
        db.execute('UPDATE stream.dynamic_feed SET sequence=%s,updated_at=clock_timestamp() WHERE id=1',(sequence,))
        temporary = clock_path.with_suffix('.dynamic.tmp')
        temporary.write_text(json.dumps(dict(last_start_ms=start,run_id=value['run_id'])),encoding='utf-8')
        temporary.replace(clock_path)  # A crash may skip clock time, never reuse an earlier window.
        return row, feed['interval_seconds']


def reserve(db, sequence, phase):
    column, state = {'data':('data_attempts','planned'),'marker':('marker_attempts','data_sent')}[phase]
    row = db.execute(f'''UPDATE stream.dynamic_batches SET {column}={column}+1
        WHERE sequence=%s AND state=%s AND {column}<2 RETURNING {column}''',(sequence,state)).fetchone()
    if not row:
        raise ApplicationError('PublicationAttemptsExhausted', non_retryable=True)


def send(events):
    from kafka import KafkaProducer
    producer = KafkaProducer(bootstrap_servers='kafka:9092', client_id=OWNER,
        acks='all', enable_idempotence=True, retries=1, max_in_flight_requests_per_connection=1,
        request_timeout_ms=8000, delivery_timeout_ms=15000, max_block_ms=5000,
        api_version_auto_timeout_ms=4000, linger_ms=10, compression_type='gzip')
    try:
        if producer.partitions_for('newsagent-synthetic-v1') != {0}:
            raise ValueError('DynamicFeedRequiresSinglePartition')
        futures = [producer.send('newsagent-synthetic-v1', partition=0, value=encode(e)) for e in events]
        deadline = time.monotonic()+20
        for future in futures:
            future.get(timeout=max(0.01, deadline-time.monotonic()))
    finally:
        producer.close(timeout=2)


def verify(db, value):
    # Reuse the independently implemented batch oracle, never the Flink accumulator.
    from tools.streaming_lab_acceptance import recount
    rows = db.execute('''SELECT s.tenant_id,s.payload_text,r.report FROM stream.snapshots s
        JOIN stream.reports r USING(snapshot_id) JOIN stream.consumption_ledger l USING(snapshot_id)
        WHERE s.run_id=%s AND s.final AND l.state='completed'
        AND NOT EXISTS(SELECT 1 FROM stream.invalid_windows i WHERE i.window_key=s.window_key)''',
        (value['run_id'],)).fetchall()
    if len(rows) != 2 or {r['tenant_id'] for r in rows} != set(TENANTS):
        return False
    for row in rows:
        expected, count = recount(value['events'], row['tenant_id'])
        body = json.loads(row['payload_text'])
        if body['metrics'] != expected or body['event_count'] != count or row['report']['external_model_calls'] != 0:
            raise ApplicationError('DynamicRecountMismatch', non_retryable=True)
    return True


@activity.defn(name='dynamic_feed_tick_v1')
def tick():
    with connect() as db:
        if not db.execute('SELECT pg_try_advisory_lock(480004) AS locked').fetchone()['locked']:
            raise RuntimeError('DynamicPublisherBusy')
        feed = db.execute('SELECT enabled FROM stream.dynamic_feed WHERE id=1').fetchone()
        if feed['enabled']:
            read_json('http://agent:8000/ready')
            jobs = read_json('http://jobmanager:8081/jobs/overview')['jobs']
            if len([j for j in jobs if j['state']=='RUNNING']) != 1:
                raise ValueError('FlinkNotRunning')
            ops = read_json('http://agent:8000/streaming-lab-api/operations')
            size = db.execute('SELECT pg_database_size(current_database()) AS size').fetchone()['size']
            enforce_budget(database_bytes=size, backlog=ops['ledger']['pending']+ops['ledger']['started'],
                           lag=ops['oldest_pending_seconds'], failures=ops['ledger']['failed'])
        info = activity.info()
        row, interval = plan(db, info.workflow_run_id+':'+info.activity_id)
        if row is None:
            return dict(interval_seconds=5, run_id=None)
        if row['state'] == 'completed':
            return dict(interval_seconds=interval, run_id=row['run_id'])
        if row.get('public_catalog_id'):
            from simulation.streaming_lab.public_news import load_catalog, public_packet, VERSION as PUBLIC_VERSION
            if row['generator_version']!=PUBLIC_VERSION:
                raise ValueError('Unsupported public generator version')
            value=public_packet(row['sequence'],row['window_start_ms'],load_catalog(db,row['public_catalog_id']))
        else:
            value = packet(row['sequence'], row['window_start_ms'], row['generator_version'])
        if digest(value) != row['packet_sha256']:
            raise ApplicationError('DynamicGeneratorVersionMismatch', non_retryable=True)
        with operation('streaming.dynamic.publish',attributes={'newsagent.run_id':row['run_id'],
                'temporal.workflow.id':WORKFLOW_ID}) as span:
            trace_id = f'{span.get_span_context().trace_id:032x}'
            db.execute('UPDATE stream.dynamic_batches SET source_trace_id=%s WHERE sequence=%s',
                       (trace_id,row['sequence']))
            if row['state'] == 'planned':
                reserve(db,row['sequence'],'data')
                send(value['events'])
                db.execute("UPDATE stream.dynamic_batches SET state='data_sent' WHERE sequence=%s",(row['sequence'],))
                row['state'] = 'data_sent'
            if row['state'] == 'data_sent':
                reserve(db,row['sequence'],'marker')
                send([value['marker']])
                db.execute("UPDATE stream.dynamic_batches SET state='marker_sent' WHERE sequence=%s",(row['sequence'],))
            deadline = time.monotonic()+45
            while not verify(db,value):
                if time.monotonic() > deadline:
                    raise TimeoutError('DynamicWindowNotConsumed')
                time.sleep(1)
            db.execute("UPDATE stream.dynamic_batches SET state='completed',completed_at=clock_timestamp() WHERE sequence=%s",(row['sequence'],))
            db.execute('UPDATE stream.dynamic_feed SET last_error=NULL,updated_at=clock_timestamp() WHERE id=1')
        return dict(interval_seconds=interval, run_id=row['run_id'])


@activity.defn(name='dynamic_feed_fail_v1')
def fail():
    with connect() as db:
        db.execute("UPDATE stream.dynamic_feed SET enabled=false,last_error='TickFailedInspectWorkflow',updated_at=clock_timestamp() WHERE id=1")


@workflow.defn(name='newsagent-dynamic-feed-v1', sandboxed=False)
class DynamicFeedWorkflow:
    @workflow.run
    async def run(self):
        for _ in range(50):
            began = workflow.now()
            try:
                result = await workflow.execute_activity(tick, start_to_close_timeout=timedelta(seconds=110),
                    retry_policy=RetryPolicy(maximum_attempts=2,initial_interval=timedelta(seconds=5)))
                interval = result['interval_seconds']
            except ActivityError:
                await workflow.execute_activity(fail,start_to_close_timeout=timedelta(seconds=15),
                    retry_policy=RetryPolicy(maximum_attempts=2))
                interval = 5
            remaining = interval-(workflow.now()-began).total_seconds()
            await workflow.sleep(max(1,remaining))
        workflow.continue_as_new()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('worker','start','pause','resume','status'))
    args = parser.parse_args()
    if args.command in ('pause','resume','status'):
        if args.command == 'resume':
            client = await Client.connect(os.environ['LAB_TEMPORAL_ADDRESS'])
            description = await client.get_workflow_handle(WORKFLOW_ID).describe(rpc_timeout=timedelta(seconds=5))
            if description.status.name != 'RUNNING':
                raise RuntimeError('Dynamic workflow is not running; inspect before resuming')
        with connect() as db:
            if args.command == 'resume':
                ensure_lock()
                db.execute('UPDATE stream.dynamic_feed SET enabled=true,last_error=NULL,updated_at=clock_timestamp() WHERE id=1')
            elif args.command == 'pause':
                db.execute('UPDATE stream.dynamic_feed SET enabled=false,updated_at=clock_timestamp() WHERE id=1')
            print(json.dumps(db.execute('SELECT * FROM stream.dynamic_feed WHERE id=1').fetchone(),default=str))
        return
    client = await Client.connect(os.environ['LAB_TEMPORAL_ADDRESS'])
    if args.command == 'start':
        # Start the fixed workflow before enabling SQL. A repeat start never creates a second loop.
        try:
            await client.start_workflow(DynamicFeedWorkflow.run,id=WORKFLOW_ID,task_queue=QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,rpc_timeout=timedelta(seconds=10))
        except WorkflowAlreadyStartedError:
            description = await client.get_workflow_handle(WORKFLOW_ID).describe(rpc_timeout=timedelta(seconds=5))
            if description.status.name != 'RUNNING':
                raise RuntimeError('Existing dynamic workflow is closed; inspect before restarting') from None
        ensure_lock()
        with connect() as db:
            db.execute('UPDATE stream.dynamic_feed SET enabled=true,last_error=NULL,updated_at=clock_timestamp() WHERE id=1')
        print(json.dumps(dict(workflow_id=WORKFLOW_ID,enabled=True)))
        return
    telemetry = configure_telemetry()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            async with Worker(client,task_queue=QUEUE,workflows=[DynamicFeedWorkflow],activities=[tick,fail],
                              activity_executor=executor,max_concurrent_activities=1):
                await asyncio.Event().wait()
    finally:
        telemetry.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
