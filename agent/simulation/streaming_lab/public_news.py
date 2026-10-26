"""Explicit public-content catalog and durable SQL projection for synthetic streaming.

Only aggregate snapshots cross databases. Frozen content is never rewritten.
Source delivery acknowledgement follows the target commit; retries verify identity.
"""
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine, inspect, text
from app.observability.telemetry import operation

from app.modules.news_storage.store import FrozenNewsStore, ReleaseNewsContentRepository
from app.modules.news_storage.tag_store import FrozenNewsTagStore
from simulation.streaming_lab.dynamic_data import packet, NEWS, TENANTS

VERSION = 'public-news-traffic-v1'
METRIC_VERSION = 'synthetic-public-news-metrics-v1'
POLICY = 'exact-body-sha256-per-content-type-v1'
FIELDS = ('impressions','clicks','unique_users','total_duration_seconds','effective_consumptions','interactions')


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def connect_target():
    return psycopg.connect(os.environ['LAB_UNIFIED_DSN'], row_factory=dict_row, connect_timeout=5,
                          options='-c statement_timeout=30000')


def initialize_target(db):
    directory = Path(__file__).resolve().parents[2]/'app/modules/news_storage'
    with db.transaction():
        db.execute('SELECT pg_advisory_xact_lock(480006)')
        db.execute('CREATE SCHEMA IF NOT EXISTS news_realtime')
        db.execute('CREATE TABLE IF NOT EXISTS news_realtime.migration (version text PRIMARY KEY,sha256 text NOT NULL)')
        for version, name in [('001','realtime.sql'),('002','realtime_002.sql'),('003','realtime_003.sql'),('004','realtime_004.sql'),('005','realtime_005.sql')]:
            source = (directory/name).read_bytes()
            checksum = sha256(source).hexdigest()
            old = db.execute('SELECT sha256 FROM news_realtime.migration WHERE version=%s',(version,)).fetchone()
            if old:
                if old['sha256'] != checksum:
                    raise ValueError('Unified realtime migration changed')
                continue
            db.execute(source.decode('utf-8'))
            db.execute('INSERT INTO news_realtime.migration VALUES(%s,%s)',(version,checksum))


def register_catalog(target, source, *, content_tenant, content_release, tag_release):
    """Publish an immutable catalog to both SQL stores; target commit comes first."""
    with target.transaction():
        target.execute('SELECT pg_advisory_xact_lock(480006)')
        release = target.execute("SELECT identity_sha256 FROM public.news_content_releases WHERE tenant_id=%s AND release_id=%s AND status='frozen'", (content_tenant,content_release)).fetchone()
        tags = target.execute("SELECT identity_sha256 FROM public.news_content_tag_releases WHERE tenant_id=%s AND content_release_id=%s AND tag_release_id=%s AND status='frozen'", (content_tenant,content_release,tag_release)).fetchone()
        if not release or not tags:
            raise ValueError('Unknown frozen content/tag release')
        rows = target.execute("""SELECT news_id,content_type,record_sha256,body_sha256,
            (extract(epoch FROM publish_time)*1000)::bigint AS publish_ms
            FROM public.news_content_deduplicated
            WHERE tenant_id=%s AND release_id=%s AND content_state='body_available'
              AND title IS NOT NULL AND url IS NOT NULL AND publish_time IS NOT NULL
              AND length(body)>=100 AND NOT item_quality_flags ? 'metadata_review_required'
            ORDER BY news_id COLLATE "C" """, (content_tenant,content_release)).fetchall()
        if len(rows)<3:
            raise ValueError('Catalog needs at least three eligible public articles')
        manifest = dict(content_tenant_id=content_tenant,content_release_id=content_release,tag_release_id=tag_release,
            content_identity_sha256=release['identity_sha256'],tag_identity_sha256=tags['identity_sha256'],
            dedup_policy=POLICY,eligibility_policy='cached-body-at-least-100-chars-reviewed-metadata-v1',
            tenants=list(TENANTS),eligible_count=len(rows),rows_sha256=digest(rows))
        catalog_id = 'public-'+digest(manifest)[:24]
        _publish_catalog(target,'news_realtime.catalog','news_realtime.catalog_news',catalog_id,manifest,rows)
    with source.transaction():
        source.execute('SELECT pg_advisory_xact_lock(480006)')
        _publish_catalog(source,'stream.public_catalog','stream.public_news',catalog_id,manifest,rows)
    return dict(catalog_id=catalog_id,manifest=manifest)


def _publish_catalog(db, table, news_table, catalog_id, manifest, rows):
    # Names are internal constants, never request values.
    old=db.execute(f'SELECT manifest FROM {table} WHERE catalog_id=%s',(catalog_id,)).fetchone()
    if old:
        actual=db.execute(f'SELECT news_id,content_type,record_sha256,body_sha256,publish_ms FROM {news_table} WHERE catalog_id=%s ORDER BY news_id COLLATE "C"',(catalog_id,)).fetchall()
        if old['manifest']!=manifest or actual!=rows:
            raise ValueError('Published catalog differs; refusing overwrite')
        return
    db.execute(f'INSERT INTO {table}(catalog_id,manifest) VALUES(%s,%s)',(catalog_id,Jsonb(manifest)))
    with db.cursor() as cursor, cursor.copy(f'COPY {news_table}(catalog_id,news_id,content_type,record_sha256,body_sha256,publish_ms) FROM STDIN') as copy:
        for row in rows:
            copy.write_row((catalog_id,*(row[k] for k in ('news_id','content_type','record_sha256','body_sha256','publish_ms'))))


def load_catalog(db, catalog_id, *, target=False):
    table, news_table = ('news_realtime.catalog','news_realtime.catalog_news') if target else ('stream.public_catalog','stream.public_news')
    row=db.execute(f'SELECT manifest FROM {table} WHERE catalog_id=%s',(catalog_id,)).fetchone()
    if not row or catalog_id!='public-'+digest(row['manifest'])[:24]:
        raise ValueError('Unregistered or corrupt public catalog')
    rows=db.execute(f'SELECT news_id,content_type,record_sha256,body_sha256,publish_ms FROM {news_table} WHERE catalog_id=%s ORDER BY news_id COLLATE "C"',(catalog_id,)).fetchall()
    if len(rows)!=row['manifest']['eligible_count'] or digest(rows)!=row['manifest']['rows_sha256']:
        raise ValueError('Public catalog row digest mismatch')
    return dict(catalog_id=catalog_id,manifest=row['manifest'],news={r['news_id']:r for r in rows})


def public_packet(sequence, start, catalog):
    eligible=[r for r in catalog['news'].values() if r['publish_ms']<=start]
    if len(eligible)<3 or catalog['manifest']['tenants']!=list(TENANTS):
        raise ValueError('Insufficient published news or unsupported tenant mapping')
    chosen=[eligible[((sequence-1)*3+i)%len(eligible)] for i in range(3)]
    mapping=dict(zip((n for n,_ in NEWS),chosen))
    value=packet(sequence,start)
    value.update(version=VERSION,run_id=f'public-{sequence:08d}',catalog_id=catalog['catalog_id'])
    events=[]
    for old in value['events']:
        row=mapping[old['news_id']]
        kind=old['event_type']
        if kind in ('read','play'): kind='read' if row['content_type']=='article' else 'play'
        event=dict(old,schema_version=2,run_id=value['run_id'],catalog_id=catalog['catalog_id'],
                   news_id=row['news_id'],content_type=row['content_type'],event_type=kind)
        # Preserve the old event ordinal while namespacing the new stream/version.
        event['event_id']=f"{value['run_id']}:{start}:{old['event_id'].split(':')[2]}:{row['news_id']}:{kind}:{old['event_id'].split(':')[-1]}"
        events.append(event)
    value['events']=events
    value['marker']=dict(value['marker'],schema_version=2,run_id=value['run_id'],catalog_id=catalog['catalog_id'])
    return value


def retained_content_binding(engine, manifest):
    """Resolve an explicitly audited prune; exact article hashes are checked below.

    Historical catalogs remain immutable. A retained article can use the new
    frozen release, while removed articles are absent there and fail closed.
    """
    if not inspect(engine).has_table('news_content_prune_runs'):
        return manifest
    with engine.connect() as connection:
        rows=connection.execute(text('SELECT target_release_id,manifest FROM news_content_prune_runs WHERE source_release_id=:release'),
                                {'release':manifest['content_release_id']}).mappings().all()
    matches=[]
    for row in rows:
        audit=row['manifest'] if isinstance(row['manifest'],dict) else json.loads(row['manifest'])
        if audit.get('tenant_id')==manifest['content_tenant_id']:
            if (audit.get('status') not in {'applied_target','applied_verified'}
                    or audit.get('content_release_id')!=row['target_release_id']
                    or not audit.get('tag_release_id')):
                raise ValueError('Prune binding is incomplete or inconsistent')
            matches.append(audit)
    if not matches:
        return manifest
    if len(matches)!=1:
        raise ValueError('Ambiguous retained content binding')
    return {**manifest,'content_release_id':matches[0]['content_release_id'],
            'tag_release_id':matches[0]['tag_release_id']}


class PublicContentRepository:
    def __init__(self, catalog, engine):
        self.catalog=catalog
        self.content_binding=retained_content_binding(engine,catalog['manifest'])
        m=self.content_binding
        self.store=FrozenNewsStore(engine)
        self.tags=FrozenNewsTagStore(self.store)
        self.delegate=ReleaseNewsContentRepository(self.store,m['content_tenant_id'],m['content_release_id'])

    async def batch_get_by_news_ids(self, *, tenant_id, news_ids):
        if tenant_id not in self.catalog['manifest']['tenants'] or any(n not in self.catalog['news'] for n in news_ids):
            raise ValueError('Content request crossed registered business/catalog scope')
        m=self.content_binding
        def verify():
            for news in news_ids:
                row=self.tags.get_record(m['content_tenant_id'],m['content_release_id'],m['tag_release_id'],news)
                if not row or any(row[k]!=self.catalog['news'][news][k] for k in ('record_sha256','body_sha256','content_type')):
                    raise ValueError('Public content/tag identity mismatch')
        with operation('streaming.sql.content',attributes={'newsagent.tenant_id':tenant_id}):
            await asyncio.to_thread(verify)
            return await self.delegate.batch_get_by_news_ids(tenant_id=m['content_tenant_id'],news_ids=news_ids)


def content_engine():
    dsn=os.environ['LAB_UNIFIED_DSN']
    return create_engine(dsn.replace('postgresql://','postgresql+psycopg://',1),
                         connect_args={'options':'-c default_transaction_read_only=on -c statement_timeout=10000'})


def project_once(source, target, *, limit=10):
    """At-least-once cross-DB delivery; immutable target insert then source ACK."""
    from simulation.streaming_lab.bridge import validate_payload
    if not source.execute('SELECT pg_try_advisory_lock(480007) AS locked').fetchone()['locked']:
        return 0
    try:
        invalid=source.execute('SELECT window_key,reason,observed_at FROM stream.invalid_windows').fetchall()
        with target.transaction():
            for r in invalid:
                target.execute('INSERT INTO news_realtime.invalid_window VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',tuple(r.values()))
            target.execute('INSERT INTO news_realtime.sync_status VALUES(1,clock_timestamp()) ON CONFLICT(id) DO UPDATE SET last_success_at=EXCLUDED.last_success_at')
        rows=source.execute("""SELECT s.*,r.report FROM stream.snapshots s JOIN stream.reports r USING(snapshot_id)
          JOIN stream.consumption_ledger l USING(snapshot_id)
          WHERE s.final AND l.state='completed' AND s.payload_text::jsonb->>'metric_version'=%s
          AND NOT EXISTS(SELECT 1 FROM stream.warehouse_delivery d WHERE d.snapshot_id=s.snapshot_id)
          ORDER BY s.published_at,s.snapshot_id LIMIT %s""",(METRIC_VERSION,limit)).fetchall()
        for r in rows:
            body=json.loads(r['payload_text'])
            with operation('streaming.sql.project',attributes={'newsagent.run_id':r['snapshot_id']}), target.transaction():
                cat=load_catalog(target,body['catalog_id'],target=True)
                validate_payload(r['payload_text'],r['snapshot_id'],r['tenant_id'],catalog=cat)
                if r['window_key']!=f"{body['tenant_id']}/{body['run_id']}/{body['window_start_ms']}" or any(r[k]!=body[k] for k in ('run_id','window_start_ms','window_end_ms','final')):
                    raise ValueError('Source SQL envelope differs from immutable payload')
                if (r['report']['snapshot_id']!=r['snapshot_id'] or r['report'].get('content_catalog_id')!=body['catalog_id']
                        or r['report'].get('tenant_id')!=body['tenant_id']
                        or r['report'].get('metric_version')!=METRIC_VERSION or r['report'].get('final') is not True):
                    raise ValueError('Report/catalog identity mismatch')
                ranking={v['news_id']:v for v in r['report']['ranking']}
                if set(ranking)!={v['news_id'] for v in body['metrics']}:
                    raise ValueError('Report metric coverage mismatch')
                for v in body['metrics']:
                    if ranking[v['news_id']]['metrics']!={k:v[k] for k in FIELDS}:
                        raise ValueError('Report counters differ from source snapshot')
                old=target.execute('SELECT payload_text,report FROM news_realtime.snapshot WHERE snapshot_id=%s',(r['snapshot_id'],)).fetchone()
                if old:
                    if old['payload_text']!=r['payload_text'] or old['report']!=r['report']:
                        raise ValueError('Target snapshot identity collision')
                else:
                    target.execute('''INSERT INTO news_realtime.snapshot(snapshot_id,catalog_id,window_key,revision,tenant_id,run_id,window_start_ms,window_end_ms,payload_text,report,source_published_at)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                      (r['snapshot_id'],body['catalog_id'],r['window_key'],r['revision'],r['tenant_id'],r['run_id'],r['window_start_ms'],r['window_end_ms'],r['payload_text'],Jsonb(r['report']),r['published_at']))
                    for v in body['metrics']:
                        target.execute('INSERT INTO news_realtime.metric VALUES('+','.join(['%s']*10)+')',
                          (r['snapshot_id'],body['catalog_id'],v['news_id'],*(v[k] for k in FIELDS),ranking[v['news_id']]['hot_score']))
            source.execute('INSERT INTO stream.warehouse_delivery(snapshot_id) VALUES(%s) ON CONFLICT DO NOTHING',(r['snapshot_id'],))
        return len(rows)
    finally:
        source.execute('SELECT pg_advisory_unlock(480007)')


projection_health={'state':'disabled','last_success_at':None,'error_type':None}


async def projection_loop(source_connect):
    def once():
        with source_connect() as source, connect_target() as target:
            return project_once(source,target)
    while True:
        try:
            await asyncio.to_thread(once)
            projection_health.update(state='ready',last_success_at=datetime.now(timezone.utc).isoformat(),error_type=None)
        except Exception as exc:
            projection_health.update(state='recovering',error_type=type(exc).__name__)
        await asyncio.sleep(3 if projection_health['state']=='ready' else 10)
