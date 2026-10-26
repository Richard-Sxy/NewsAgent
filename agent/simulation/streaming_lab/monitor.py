"""Durable one-minute sliding discovery, explicitly registered per source session.

Publication commits before cursor advancement. A crash between them safely
replays the identical immutable snapshot; no cursor is advanced on a data gap.
"""
import asyncio
from datetime import datetime, timezone
import time

import psycopg

from app.modules.hot_news.scoring.discovery import VERSION, policy_for
from simulation.streaming_lab.public_news import digest, load_catalog
from simulation.streaming_lab.window_query import MINUTE, WindowUnavailable, publish_window, validate_request

POLL_SECONDS = 5
MAX_PENDING = 100


def register(db, *, tenant_id, session_id, window_start_ms, window_minutes, discovery_policy=VERSION):
    validate_request(tenant_id, session_id, window_start_ms, window_minutes)
    policy_for(discovery_policy)
    identity = dict(tenant_id=tenant_id, session_id=session_id,
                    window_minutes=window_minutes, policy_version=discovery_policy)
    mid = digest(identity)
    with db.transaction():
        session = db.execute('SELECT * FROM stream.wallclock_session WHERE id=%s', (session_id,)).fetchone()
        if session is None or window_start_ms < session['start_ms']:
            raise ValueError('Monitor start must belong to an existing source session')
        catalog = load_catalog(db, session['catalog_id'])
        if tenant_id not in catalog['manifest']['tenants']:
            raise ValueError('Monitor tenant is outside the source catalog')
        db.execute('''INSERT INTO stream.hot_monitors(monitor_id,tenant_id,session_id,window_minutes,
            policy_version,initial_start_ms,next_start_ms) VALUES(%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING''', (mid,tenant_id,session_id,window_minutes,discovery_policy,window_start_ms,window_start_ms))
        row = db.execute('SELECT * FROM stream.hot_monitors WHERE monitor_id=%s', (mid,)).fetchone()
        if row['initial_start_ms'] != window_start_ms:
            raise ValueError('Existing monitor has a different initial start; cursor cannot be reset')
    return row


def set_enabled(db, monitor_id, enabled):
    with db.transaction():
        # Serializes with publication, including the publication/cursor crash gap.
        db.execute('SELECT pg_advisory_xact_lock(%s)', (int(monitor_id[:15],16),))
        return db.execute('''UPDATE stream.hot_monitors SET enabled=%s,
            status=CASE WHEN %s THEN 'waiting_source' ELSE 'paused' END,
            next_poll_at=clock_timestamp(),updated_at=clock_timestamp()
            WHERE monitor_id=%s RETURNING *''', (enabled,enabled,monitor_id)).fetchone()


def _status(db, mid, status, source_end, error=None):
    db.execute('''UPDATE stream.hot_monitors SET status=%s,source_end_ms=%s,error_type=%s,
        checked_at=clock_timestamp(),updated_at=clock_timestamp(),
        next_poll_at=clock_timestamp()+(%s * interval '1 second') WHERE monitor_id=%s''',
        (status,source_end,error,POLL_SECONDS,mid))


def process_one(db, monitor_id):
    lock = int(monitor_id[:15],16)
    if not db.execute('SELECT pg_try_advisory_lock(%s) AS locked', (lock,)).fetchone()['locked']:
        return False
    try:
        row = db.execute('SELECT * FROM stream.hot_monitors WHERE monitor_id=%s', (monitor_id,)).fetchone()
        if row is None or not row['enabled'] or row['status'] == 'blocked':
            return False
        source = db.execute('''SELECT max(s.window_end_ms) AS end_ms
            FROM stream.reader_window w JOIN stream.window_heads h USING(window_key)
            JOIN stream.snapshots s ON s.snapshot_id=h.snapshot_id
            WHERE w.session_id=%s AND w.tenant_id=%s AND w.final AND s.final
              AND w.watermark_ms>=s.window_end_ms+30000
              AND NOT EXISTS(SELECT 1 FROM stream.invalid_snapshots i WHERE i.snapshot_id=s.snapshot_id)''',
            (row['session_id'],row['tenant_id'])).fetchone()['end_ms']
        now_ms = int(datetime.now(timezone.utc).timestamp()*1000)
        if row['next_start_ms'] < now_ms-86_400_000:
            _status(db, monitor_id, 'blocked', source, 'HistoryRetentionExceeded')
            return False
        if source is None or source < row['next_start_ms']+row['window_minutes']*MINUTE:
            _status(db, monitor_id, 'waiting_source', source, 'SourceNotFinal')
            return False
        pending = db.execute("SELECT count(*) AS n FROM stream.consumption_ledger WHERE state IN ('pending','started')").fetchone()['n']
        if pending >= MAX_PENDING:
            _status(db, monitor_id, 'backpressure', source, 'PendingLimitReached')
            return False
        try:
            saved = publish_window(db, tenant_id=row['tenant_id'], session_id=row['session_id'],
                window_start_ms=row['next_start_ms'], window_minutes=row['window_minutes'],
                discovery_policy=row['policy_version'])
        except WindowUnavailable:
            _status(db, monitor_id, 'waiting_source', source, 'IncompleteWindowOrBaseline')
            return False
        except ValueError:
            _status(db, monitor_id, 'blocked', source, 'InvalidSourceOrPolicy')
            return False
        with db.transaction():
            db.execute('''INSERT INTO stream.hot_monitor_windows(monitor_id,window_start_ms,snapshot_id)
                VALUES(%s,%s,%s) ON CONFLICT DO NOTHING''', (monitor_id,row['next_start_ms'],saved['snapshot_id']))
            db.execute('''UPDATE stream.hot_monitors SET next_start_ms=%s,last_snapshot_id=%s,
                source_end_ms=%s,status='published',error_type=NULL,checked_at=clock_timestamp(),
                updated_at=clock_timestamp(),next_poll_at=clock_timestamp()+(%s * interval '1 second')
                WHERE monitor_id=%s''', (row['next_start_ms']+MINUTE,saved['snapshot_id'],source,POLL_SECONDS,monitor_id))
        return True
    finally:
        db.execute('SELECT pg_advisory_unlock(%s)', (lock,))


def tick(connect):
    with connect() as db:
        row = db.execute('''SELECT monitor_id FROM stream.hot_monitors
            WHERE enabled AND status<>'blocked' AND next_poll_at<=clock_timestamp()
            ORDER BY next_poll_at,monitor_id LIMIT 1''').fetchone()
        if row is None:
            return False
        process_one(db,row['monitor_id'])
        return True


def status(db, tenant_id):
    rows = db.execute('''SELECT m.*,l.state AS last_analysis_state,
        l.error_type AS last_analysis_error,r.created_at AS last_report_at,
        EXISTS(SELECT 1 FROM stream.invalid_snapshots i WHERE i.snapshot_id=m.last_snapshot_id) AS last_invalidated,
        (SELECT count(*) FROM stream.hot_monitor_windows w JOIN stream.consumption_ledger q USING(snapshot_id)
          WHERE w.monitor_id=m.monitor_id AND q.state='failed') AS failed_windows,
        (SELECT count(*) FROM stream.hot_monitor_windows w JOIN stream.invalid_snapshots i USING(snapshot_id)
          WHERE w.monitor_id=m.monitor_id) AS invalidated_windows,
        (SELECT max(s.window_end_ms) FROM stream.hot_monitor_windows w
          JOIN stream.snapshots s USING(snapshot_id) JOIN stream.reports r USING(snapshot_id)
          WHERE w.monitor_id=m.monitor_id AND NOT EXISTS
            (SELECT 1 FROM stream.invalid_snapshots i WHERE i.snapshot_id=w.snapshot_id)) AS last_completed_end_ms
        FROM stream.hot_monitors m LEFT JOIN stream.consumption_ledger l ON l.snapshot_id=m.last_snapshot_id
        LEFT JOIN stream.reports r ON r.snapshot_id=m.last_snapshot_id
        WHERE m.tenant_id=%s ORDER BY m.monitor_id LIMIT 100''', (tenant_id,)).fetchall()
    now_ms = int(datetime.now(timezone.utc).timestamp()*1000)
    for row in rows:
        source_end = row['source_end_ms']
        row['source_lag_seconds'] = None if source_end is None else max(0,(now_ms-source_end)/1000)
        row['backlog_windows'] = 0 if source_end is None else max(0,
            (source_end-row['next_start_ms']-row['window_minutes']*MINUTE)//MINUTE+1)
        row['report_lag_seconds'] = None if row['last_completed_end_ms'] is None else max(0,
            (now_ms-row['last_completed_end_ms'])/1000)
    return rows


async def run_monitors(connect, health, *, sleep=asyncio.sleep):
    while True:
        try:
            worked = await asyncio.to_thread(tick,connect)
            health.state='ready';health.error_type=None;health.consecutive_failures=0
            health.last_success_at=datetime.now(timezone.utc).isoformat();health.last_tick=time.monotonic()
            await sleep(0.05 if worked else 1)
        except (psycopg.OperationalError,psycopg.InterfaceError,ConnectionError,TimeoutError) as exc:
            health.state='recovering';health.error_type=type(exc).__name__
            health.consecutive_failures+=1;health.last_tick=time.monotonic()
            await sleep(min(15,2**min(health.consecutive_failures-1,4)))
