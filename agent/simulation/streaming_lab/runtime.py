"""Isolated Temporal consumption worker, durable dispatch ledger and loopback read API."""
import asyncio
from datetime import timedelta
import json
import os
import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, StrictInt, StrictBool, Field
from typing import Literal
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.common import RetryPolicy
from temporalio.service import RPCError
from app.observability.telemetry import configure_telemetry, operation
from simulation.streaming_lab.bridge import analyze_snapshot, TENANTS
from simulation.streaming_lab.sql_retrieval import SqlRelatedNewsSearchClient
from simulation.streaming_lab.migrations import migrate
from simulation.streaming_lab.dispatcher import SqlLedger, TemporalTransport, DispatcherHealth, run_dispatcher
from simulation.streaming_lab.public_news import (
    projection_loop, projection_health, connect_target, load_catalog,
    PublicContentRepository, content_engine,
)

TASK_QUEUE = 'newsagent-stream-snapshots-v1'
app = FastAPI(title='NewsAgent synthetic streaming lab', docs_url='/docs')
temporal_client = None
dispatcher_health = DispatcherHealth()
monitor_health = DispatcherHealth()
UI_DIR = Path(os.environ.get('LAB_UI_DIR', '/lab-ui'))


@app.get('/workbench/config.json')
def workbench_config():
    return {'appTitle':'NewsAgent 流式实验', 'environment':'streaming-lab', 'enableStreamingLab':True}


app.mount('/workbench/assets', StaticFiles(directory=UI_DIR/'assets', check_dir=False), name='lab-assets')


@app.get('/workbench/{path:path}', include_in_schema=False)
def workbench(path: str):
    if '.' in path or not (UI_DIR/'index.html').is_file():
        raise HTTPException(404,'Workbench build not found')
    return FileResponse(UI_DIR/'index.html', headers={'Cache-Control':'no-store'})


def connect():
    return psycopg.connect(os.environ['LAB_DSN'], autocommit=True, row_factory=dict_row,
                          connect_timeout=5, options='-c statement_timeout=10000')


def load_snapshot(snapshot_id):
    if not re.fullmatch('[0-9a-f]{64}', snapshot_id):
        raise ValueError('Invalid snapshot ID')
    with connect() as db, db.transaction():
        db.execute('SET TRANSACTION READ ONLY')
        row = db.execute('SELECT s.* FROM stream.snapshots s WHERE snapshot_id=%s AND NOT EXISTS (SELECT 1 FROM stream.invalid_snapshots i WHERE i.snapshot_id=s.snapshot_id)', (snapshot_id,)).fetchone()
        if not row:
            raise ValueError('Snapshot missing or invalidated')
        return row


@activity.defn(name='analyze_stream_snapshot_v1')
async def analyze_activity(snapshot_id: str) -> dict:
    workflow_id = activity.info().workflow_id
    with connect() as db:
        old = db.execute('SELECT report FROM stream.reports WHERE snapshot_id=%s',(snapshot_id,)).fetchone()
    if old:
        load_snapshot(snapshot_id)  # A late conflict must not revive an invalidated report.
        return {'snapshot_id':snapshot_id, 'run_key':old['report']['run_key'], 'status':'completed'}
    with operation('streaming.snapshot', attributes={'newsagent.run_id':snapshot_id,'temporal.workflow.id':workflow_id}) as span:
        row = load_snapshot(snapshot_id)
        body=json.loads(row['payload_text'])
        from simulation.streaming_lab.window_query import VERSION as WINDOW_VERSION
        if body.get('metric_version') in ('synthetic-public-news-metrics-v1',WINDOW_VERSION):
            with connect() as db:
                catalog=load_catalog(db,body['catalog_id'])
            engine=content_engine()
            readers=None
            try:
                if body['metric_version']==WINDOW_VERSION:
                    from simulation.streaming_lab.reader_port import service
                    readers=service(body['reader_context'],body['reader_snapshot'])
                elif row['final'] and row['run_id'].startswith('wc-'):
                    from simulation.streaming_lab.reader_port import service
                    # The two consumers share one Kafka source. Wait for aggregate reconciliation,
                    # never substitute the historical frozen package or a stale minute.
                    audience=None
                    for _ in range(16):
                        with connect_target() as target:
                            audience=target.execute('SELECT * FROM news_realtime.reader_final WHERE window_key=%s',(row['window_key'],)).fetchone()
                        if audience:
                            break
                        await asyncio.sleep(1)
                    if not audience:
                        raise RuntimeError('Realtime reader window is not finalized')
                    with connect() as db:
                        session=db.execute('SELECT * FROM stream.wallclock_session WHERE id=%s',(audience['session_id'],)).fetchone()
                    readers=service(session,audience)
                report=await analyze_snapshot(row['payload_text'],snapshot_id,row['tenant_id'],catalog=catalog,
                    content_repository=PublicContentRepository(catalog,engine),reader_service=readers,
                    related_search=SqlRelatedNewsSearchClient(engine,catalog,tenant_id=row['tenant_id'],as_of_ms=body['window_end_ms']))
            finally:
                if readers:
                    await readers.close()
                engine.dispose()
        else:
            report = await analyze_snapshot(row['payload_text'],snapshot_id,row['tenant_id'])
        trace_id = f'{span.get_span_context().trace_id:032x}'
        report.update(trace_id=trace_id,workflow_id=workflow_id,revision=row['revision'])
        with connect() as db, db.transaction():
            if db.execute('SELECT 1 FROM stream.invalid_snapshots WHERE snapshot_id=%s',(snapshot_id,)).fetchone():
                raise ValueError('Snapshot invalidated during analysis')
            db.execute('INSERT INTO stream.reports(snapshot_id,run_key,workflow_id,trace_id,report) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(snapshot_id) DO NOTHING',
                       (snapshot_id,report['run_key'],workflow_id,trace_id,Jsonb(report)))
            db.execute("UPDATE stream.consumption_ledger SET state='completed',updated_at=clock_timestamp() WHERE snapshot_id=%s",(snapshot_id,))
        return {'snapshot_id':snapshot_id,'run_key':report['run_key'],'status':'completed'}


@workflow.defn(name='newsagent-stream-snapshot-v1', sandboxed=False)
class SnapshotWorkflow:
    @workflow.run
    async def run(self, snapshot_id: str) -> dict:
        return await workflow.execute_activity(analyze_activity,snapshot_id,
            start_to_close_timeout=timedelta(minutes=2),schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=2,initial_interval=timedelta(seconds=3)))


async def dispatch_loop(client):
    await run_dispatcher(SqlLedger(connect),TemporalTransport(client,TASK_QUEUE),dispatcher_health)


class WindowQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tenant_id: str
    session_id: str = Field(pattern=r'^[a-z0-9-]{1,64}$')
    window_start_ms: StrictInt = Field(gt=0)
    window_minutes: Literal[5,15]
    discovery_policy: Literal['hot-discovery-v2'] | None = None


@app.post('/streaming-lab-api/window-queries', status_code=202)
def query_window(body: WindowQuery):
    from simulation.streaming_lab.window_query import publish_window, WindowUnavailable
    try:
        with connect() as db:
            return publish_window(db,**body.model_dump())
    except WindowUnavailable as exc:
        raise HTTPException(409,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


class HotMonitorQuery(WindowQuery):
    discovery_policy: Literal['hot-discovery-v2'] = 'hot-discovery-v2'


class HotMonitorEnabled(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool


@app.post('/streaming-lab-api/hot-monitors', status_code=201)
def register_monitor(body: HotMonitorQuery):
    from simulation.streaming_lab.monitor import register
    try:
        with connect() as db:
            return register(db,**body.model_dump())
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


@app.put('/streaming-lab-api/hot-monitors/{monitor_id}')
def enable_monitor(monitor_id: str, body: HotMonitorEnabled):
    from simulation.streaming_lab.monitor import set_enabled
    if not re.fullmatch('[0-9a-f]{64}',monitor_id):
        raise HTTPException(404,'Monitor not found')
    with connect() as db:
        row=set_enabled(db,monitor_id,body.enabled)
    if row is None:
        raise HTTPException(404,'Monitor not found')
    return row


@app.get('/streaming-lab-api/hot-monitors')
def monitor_status(tenant_id: str):
    from simulation.streaming_lab.monitor import status
    if tenant_id not in TENANTS:
        raise HTTPException(400,'Unknown synthetic tenant')
    with connect() as db:
        rows=status(db,tenant_id)
    return {'monitors':rows,'loop':monitor_health.public(),'scope':'isolated_synthetic'}


# The main API binds gateway tenant/permissions before calling these isolated endpoints.
from app.modules.hot_news.contracts.monitor import MonitorStart, MonitorSwitch


def monitor_feed_projection(db, tenant_id):
    from simulation.streaming_lab.notifications import feed
    value = feed(db, tenant_id)
    if monitor_health.public()['state'] != 'ready':
        value.source_state = 'unavailable'
        value.items = []
    return value


@app.get('/streaming-lab-api/hot-monitor-feed')
def hot_monitor_feed(tenant_id: str):
    if tenant_id not in TENANTS:
        raise HTTPException(404, 'Unknown tenant')
    with connect() as db:
        return monitor_feed_projection(db, tenant_id)


@app.post('/streaming-lab-api/hot-monitor-feed')
def start_hot_monitor(body: MonitorStart, tenant_id: str):
    from simulation.streaming_lab.notifications import start_latest
    if tenant_id not in TENANTS:
        raise HTTPException(404, 'Unknown tenant')
    try:
        with connect() as db:
            start_latest(db, tenant_id, body.window_minutes)
            return monitor_feed_projection(db, tenant_id)
    except ValueError as exc:
        raise HTTPException(409, 'No active source session or compatible monitor') from exc


@app.put('/streaming-lab-api/hot-monitor-feed/{monitor_id}')
def switch_hot_monitor(monitor_id: str, body: MonitorSwitch, tenant_id: str):
    from simulation.streaming_lab.notifications import switch_owned
    if tenant_id not in TENANTS or not re.fullmatch('[0-9a-f]{64}', monitor_id):
        raise HTTPException(404, 'Monitor not found')
    with connect() as db:
        if switch_owned(db, tenant_id, monitor_id, body.enabled) is None:
            raise HTTPException(404, 'Monitor not found')
        return monitor_feed_projection(db, tenant_id)


def operational_snapshot():
    with connect() as db, db.transaction():
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        states=db.execute('SELECT state,count(*) AS count FROM stream.consumption_ledger GROUP BY state').fetchall()
        lag=db.execute("SELECT coalesce(max(extract(epoch from clock_timestamp()-s.published_at)),0) AS oldest_pending_seconds FROM stream.snapshots s JOIN stream.consumption_ledger l USING(snapshot_id) WHERE l.state IN ('pending','started')").fetchone()
        latency=db.execute('''SELECT count(*) AS report_count,
            percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP (ORDER BY extract(epoch from r.created_at-s.published_at)) AS seconds
            FROM stream.reports r JOIN stream.snapshots s USING(snapshot_id)''').fetchone()
        counts=db.execute('SELECT (SELECT count(*) FROM stream.invalid_windows) AS invalid_windows,(SELECT count(*) FROM stream.rejections) AS rejected_events,(SELECT count(*) FROM stream.backfills) AS backfills').fetchone()
        feed=db.execute('SELECT enabled,interval_seconds,max_windows,sequence,last_error,updated_at,public_catalog_id FROM stream.dynamic_feed WHERE id=1').fetchone()
        batches=db.execute('SELECT sequence,run_id,generator_version,scenario,state,event_count,window_start_ms,source_trace_id,created_at,completed_at FROM stream.dynamic_batches ORDER BY sequence DESC LIMIT 12').fetchall()
        wallclock=db.execute('''SELECT s.id,s.enabled,s.start_ms,s.last_second,s.last_error,s.updated_at,
            r.next_offset,r.watermark_ms,r.updated_at AS reader_updated_at,
            extract(epoch FROM clock_timestamp()-r.updated_at) AS reader_lag_seconds
            FROM stream.wallclock_session s LEFT JOIN stream.reader_offset r ON r.session_id=s.id
            ORDER BY s.start_ms DESC LIMIT 1''').fetchone()
        invalidated_failures=db.execute('''SELECT count(*) AS n FROM stream.consumption_ledger l
            JOIN stream.snapshots s USING(snapshot_id) JOIN stream.invalid_windows i USING(window_key)
            WHERE l.state='failed' ''').fetchone()['n']
    return dict(scope='isolated_synthetic',dispatcher=dispatcher_health.public(),hot_monitor=monitor_health.public(),
                ledger={state:next((r['count'] for r in states if r['state']==state),0) for state in ('pending','started','completed','failed')},
                oldest_pending_seconds=float(lag['oldest_pending_seconds']),
                publication_to_report=latency,dynamic_feed={**feed,'recent_batches':batches},wallclock=wallclock,
                invalidated_failures=invalidated_failures,public_warehouse=dict(projection_health),**counts)


@app.get('/streaming-lab-api/public-news')
def public_news(tenant_id: str, limit: int = 20):
    if tenant_id not in TENANTS or not 1<=limit<=100:
        raise HTTPException(400,'Invalid tenant or query limit')
    if not os.environ.get('LAB_UNIFIED_DSN'):
        raise HTTPException(503,'Unified warehouse is not configured')
    with connect_target() as db:
        db.execute('SET TRANSACTION READ ONLY')
        rows=db.execute('''SELECT snapshot_id,revision,tenant_id,run_id,news_id,title,content_state,
          window_start,window_end,topic_ids,label_status,impressions,clicks,unique_users,hot_score,
          metric_source_mode,content_source_mode,reader_profile_status,source_checked_at,source_check_fresh
          FROM news_realtime.news_detail_current WHERE tenant_id=%s
          ORDER BY window_start DESC,hot_score DESC,news_id LIMIT %s''',(tenant_id,limit)).fetchall()
    return {'items':rows,'limit':limit,'projection':dict(projection_health)}


@app.get('/streaming-lab-api/realtime-news')
def realtime_news(tenant_id: str, limit: int = 20):
    if tenant_id not in TENANTS or not 1<=limit<=100:
        raise HTTPException(400,'Invalid tenant or limit')
    with connect_target() as db:
        db.execute('SET TRANSACTION READ ONLY')
        rows=db.execute('''SELECT window_key,revision,tenant_id,run_id,news_id,title,window_start,window_end,
            impressions,clicks,unique_users,reader_count,reader_profile,reader_profile_status,final,
            watermark,last_event_at,projected_at,source_check_fresh,latest_event_to_sql_seconds,event_time_mode
            FROM news_realtime.news_live_current WHERE tenant_id=%s
            ORDER BY window_start DESC,news_id LIMIT %s''',(tenant_id,limit)).fetchall()
        topics=db.execute('''SELECT t.policy->'catalog'->'topics' AS topics FROM public.news_content_tag_releases t
            JOIN news_realtime.catalog c ON c.manifest->>'content_tenant_id'=t.tenant_id
            AND c.manifest->>'tag_release_id'=t.tag_release_id ORDER BY c.created_at DESC LIMIT 1''').fetchone()
    return {'items':rows,'limit':limit,'refresh_seconds':5,'source_mode':'synthetic',
            'topic_names':{t['topic_id']:t['name'] for t in topics['topics']} if topics else {}}


@app.get('/live')
def live():
    return {'status':'alive','scope':'isolated_synthetic'}


@app.get('/streaming-lab-api/operations')
def operations():
    try: return operational_snapshot()
    except (psycopg.OperationalError,psycopg.InterfaceError):
        raise HTTPException(503,{'dependency':'postgres','dispatcher':dispatcher_health.public()}) from None


@app.get('/metrics', include_in_schema=False)
def metrics():
    result=operations()
    lines=['# TYPE newsagent_stream_ledger gauge']
    lines += [f'newsagent_stream_ledger{{state="{state}"}} {count}' for state,count in result['ledger'].items()]
    lines += [f'newsagent_stream_dispatch_ready {int(result["dispatcher"]["state"]=="ready")}',
              f'newsagent_stream_oldest_pending_seconds {result["oldest_pending_seconds"]}',
              f'newsagent_stream_rejected_events {result["rejected_events"]}',
              f'newsagent_stream_invalid_windows {result["invalid_windows"]}',
              f'newsagent_stream_backfills {result["backfills"]}']
    return Response('\n'.join(lines)+'\n',media_type='text/plain; version=0.0.4')


@app.get('/streaming-lab-api/snapshots/{snapshot_id}/dispatch')
def dispatch_evidence(snapshot_id: str):
    if not re.fullmatch('[0-9a-f]{64}',snapshot_id): raise HTTPException(400,'Invalid snapshot ID')
    with connect() as db:
        rows=db.execute('SELECT action,attempt,error_type,occurred_at FROM stream.dispatch_events WHERE snapshot_id=%s ORDER BY id DESC LIMIT 50',(snapshot_id,)).fetchall()
    return {'snapshot_id':snapshot_id,'events':list(reversed(rows)),'limit':50}


@app.get('/ready')
async def ready():
    def probe():
        with connect() as db: db.execute('SELECT 1').fetchone()
    try:
        await asyncio.to_thread(probe)
        if temporal_client is None or not await asyncio.wait_for(temporal_client.service_client.check_health(timeout=timedelta(seconds=2)),timeout=2.5):
            raise ConnectionError('Temporal not ready')
    except (psycopg.OperationalError,psycopg.InterfaceError,RPCError,ConnectionError,TimeoutError) as exc:
        raise HTTPException(503,{'status':'recovering','error_type':type(exc).__name__}) from None
    if dispatcher_health.public()['state']!='ready':
        raise HTTPException(503,{'dispatcher':dispatcher_health.public()})
    if monitor_health.public()['state']!='ready':
        raise HTTPException(503,{'hot_monitor':monitor_health.public()})
    if os.environ.get('LAB_UNIFIED_DSN') and projection_health['state']!='ready':
        raise HTTPException(503,{'public_warehouse':dict(projection_health)})
    return {'status':'ok','scope':'isolated_synthetic','external_model_calls':0}


@app.get('/streaming-lab-api/runs')
def list_runs():
    with connect() as db:
        rows=db.execute('SELECT run_id,max(published_at) AS updated_at,count(*) AS snapshot_count FROM stream.snapshots GROUP BY run_id ORDER BY max(published_at) DESC,run_id LIMIT 50').fetchall()
    return {'runs':rows,'limit':50,'scope':'isolated_synthetic'}


@app.get('/runs/{run_id}')
@app.get('/streaming-lab-api/runs/{run_id}')
def runs(run_id: str, tenant_id: str | None = None):
    if not re.fullmatch('[a-z0-9-]{1,64}',run_id):
        raise HTTPException(400,'Invalid run ID')
    if tenant_id is not None and tenant_id not in TENANTS:
        raise HTTPException(400,'Unknown synthetic tenant')
    with connect() as db:
        rows=db.execute('''SELECT s.snapshot_id,s.window_key,s.tenant_id,s.revision,s.final,s.published_at,s.payload_text,
            l.state,l.workflow_id,l.error_type,r.report, s.snapshot_id=h.snapshot_id AS is_latest,
            b.base_snapshot_id,b.request_id AS backfill_request_id,b.added_event_count,b.reason AS backfill_reason,
            EXISTS(SELECT 1 FROM stream.invalid_snapshots i WHERE i.snapshot_id=s.snapshot_id) AS invalidated
            FROM stream.snapshots s JOIN stream.window_heads h USING(window_key)
            JOIN stream.consumption_ledger l ON l.snapshot_id=s.snapshot_id
            LEFT JOIN stream.reports r ON r.snapshot_id=s.snapshot_id
            LEFT JOIN stream.backfills b ON b.snapshot_id=s.snapshot_id
            WHERE s.run_id=%s AND (%s::text IS NULL OR s.tenant_id=%s)
            ORDER BY s.tenant_id,s.window_start_ms,s.revision LIMIT 501''',(run_id,tenant_id,tenant_id)).fetchall()
        if len(rows)>500:
            raise HTTPException(413,'Run exceeds 500 revisions; use a smaller synthetic run')
        rejections=db.execute("SELECT split_part(window_key,'/',1) AS tenant_id,event_id,reason,observed_at FROM stream.rejections WHERE split_part(window_key,'/',2)=%s AND (%s::text IS NULL OR split_part(window_key,'/',1)=%s) ORDER BY observed_at LIMIT 501",(run_id,tenant_id,tenant_id)).fetchall()
    for row in rows:
        row['snapshot']=json.loads(row.pop('payload_text'))
    return {'run_id':run_id,'snapshots':rows,'rejections':rejections[:500],'rejections_truncated':len(rejections)>500}


async def main():
    global temporal_client
    telemetry=configure_telemetry()
    with connect() as db:
        migrate(db)
    temporal_client=await Client.connect(os.environ['LAB_TEMPORAL_ADDRESS'])
    worker=Worker(temporal_client,task_queue=TASK_QUEUE,workflows=[SnapshotWorkflow],activities=[analyze_activity],max_concurrent_activities=2)
    server=uvicorn.Server(uvicorn.Config(app,host='0.0.0.0',port=8000,log_level='warning',access_log=False))
    try:
        async with worker:
            from simulation.streaming_lab.monitor import run_monitors
            tasks=[asyncio.create_task(server.serve()),asyncio.create_task(dispatch_loop(temporal_client)),
                   asyncio.create_task(run_monitors(connect,monitor_health))]
            if os.environ.get('LAB_UNIFIED_DSN'):
                tasks.append(asyncio.create_task(projection_loop(connect)))
            done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
            for task in done:
                task.result()
    finally:
        telemetry.shutdown()


if __name__=='__main__':
    asyncio.run(main())
