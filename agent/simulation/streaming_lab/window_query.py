"""Bounded 5/15-minute queries over one consistent synthetic source snapshot.

Only this lab reads synthetic membership state. Published payloads contain
aggregates and immutable source references, never user IDs or event rows.
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re

from app.analytics.entities import ContentType
from app.modules.hot_news.scoring.baseline import NewsMetricBaseline
from app.modules.reader_profiles.contracts import ReaderProfileSummary
from simulation.streaming_lab.public_news import digest, load_catalog
from simulation.streaming_lab.realtime_readers import summaries
from simulation.streaming_lab.wallclock_data import run_id

VERSION = 'synthetic-public-news-window-metrics-v1'
BASELINE_POLICY = 'previous-equal-window-v1'
MINUTE = 60_000
FIELDS = ('impressions', 'clicks', 'unique_users', 'total_duration_seconds',
          'effective_consumptions', 'interactions')


class WindowUnavailable(ValueError):
    """Known, user-visible data coverage failure; no query is published."""


def validate_request(tenant_id, session_id, start, minutes):
    from simulation.streaming_lab.bridge import TENANTS
    if (tenant_id not in TENANTS or not isinstance(session_id, str)
            or not re.fullmatch('[a-z0-9-]{1,64}', session_id)
            or type(start) is not int or start <= 0 or start % MINUTE
            or type(minutes) is not int or minutes not in (5, 15)):
        raise ValueError('Use a registered tenant, session and minute-aligned 5/15-minute window')


def _utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _metrics(buckets, clicks, start, end):
    totals = {}
    members = defaultdict(set)
    minute_members = defaultdict(set)
    for row in clicks:
        if start <= row['window_ms'] < end:
            members[row['news_id']].add(row['user_id'])
            minute_members[row['window_ms'], row['news_id']].add(row['user_id'])
    expected = set()
    for bucket in buckets:
        body = bucket['body']
        for row in body['metrics']:
            key = body['window_start_ms'], row['news_id']
            expected.add(key)
            if len(minute_members[key]) != row['unique_users']:
                raise WindowUnavailable('Exact click membership is unavailable or differs from the minute snapshot')
            total = totals.setdefault(row['news_id'], dict(news_id=row['news_id'],
                content_type=row['content_type'], **{field: 0 for field in FIELDS}))
            if total['content_type'] != row['content_type']:
                raise WindowUnavailable('Content type changed inside the requested window')
            for field in FIELDS:
                if field != 'unique_users':
                    total[field] += row[field]
    if any(key not in expected and users for key, users in minute_members.items()):
        raise WindowUnavailable('Click membership contains unrequested minute/news identities')
    for news, row in totals.items():
        row['unique_users'] = len(members[news])
    return [totals[news] for news in sorted(totals)]


def _buckets(rows, session, catalog, tenant, start, end):
    from simulation.streaming_lab.bridge import validate_payload
    lookup = {row['window_start_ms']: row for row in rows if start <= row['window_start_ms'] < end}
    if len(lookup) != len([row for row in rows if start <= row['window_start_ms'] < end]):
        raise WindowUnavailable('Multiple latest snapshots for one source minute')
    result = []
    for stamp in range(start, end, MINUTE):
        row = lookup.get(stamp)
        if row is None:
            raise WindowUnavailable('A required minute is missing')
        if (row['invalidated'] or not row['final'] or row['reader_final'] is not True
                or row['reader_watermark'] < stamp + 90_000):
            raise WindowUnavailable('The latest minute or its reader state is not complete')
        body = validate_payload(row['payload_text'], row['snapshot_id'], tenant, catalog=catalog)
        if (body['metric_version'] != 'synthetic-public-news-metrics-v1'
                or body['run_id'] != run_id(session, stamp) or not body['final']
                or body['window_start_ms'] != stamp or body['window_end_ms'] != stamp + MINUTE
                or body['event_count'] != row['reader_event_count']):
            raise WindowUnavailable('Metric and reader minute identities differ')
        result.append({**row, 'body': body})
    return result


def _references(buckets):
    return [dict(window_start_ms=r['window_start_ms'], snapshot_id=r['snapshot_id'],
                 revision=r['revision'], reader_revision=r['reader_revision']) for r in buckets]


def compose(session, catalog, tenant, start, minutes, rows, clicks, visits, history, discovery_policy=None):
    """Pure composition after all arguments were read in the same transaction."""
    validate_request(tenant, session['id'], start, minutes)
    end = start + minutes * MINUTE
    if start < session['start_ms'] or catalog['catalog_id'] != session['catalog_id']:
        raise WindowUnavailable('Requested window is outside its source session')
    current = _buckets(rows, session, catalog, tenant, start, end)
    metrics = _metrics(current, clicks, start, end)
    before = start - minutes * MINUTE
    baseline = dict(policy=BASELINE_POLICY, window_start_ms=before, window_end_ms=start,
                    status='unavailable_before_session', sources=[], metrics=[])
    if before >= session['start_ms']:
        previous = _buckets(rows, session, catalog, tenant, before, start)
        baseline.update(status='complete', sources=_references(previous),
                        metrics=_metrics(previous, clicks, before, start))
        # Absence is zero only for an eligible article in a fully covered window.
        present = {r['news_id'] for r in baseline['metrics']}
        baseline['metrics'].extend(dict(news_id=r['news_id'], content_type=r['content_type'],
            **{field: 0 for field in FIELDS}) for r in metrics
            if r['news_id'] not in present and catalog['news'][r['news_id']]['publish_ms'] <= before)
        baseline['metrics'].sort(key=lambda r: r['news_id'])
    # Synthetic identities are scoped before entering the existing causal builder.
    visits = [v for v in visits if v['tenant_id'] == tenant and start <= v['window_ms'] < end]
    items = summaries(session, tenant, start, [r['news_id'] for r in metrics], visits, history)
    audience = dict(tenant_id=tenant, start_ms=start, end_ms=end, revision=1,
        watermark_ms=current[-1]['reader_watermark'],
        generated_at=max(r['reader_generated_at'] for r in current).isoformat(), items=items)
    audience['identity_sha256'] = digest(audience)
    body = dict(metric_version=VERSION, tenant_id=tenant, catalog_id=catalog['catalog_id'],
        run_id='window-' + digest(dict(session=session['id'], start=start, minutes=minutes))[:32],
        window_start_ms=start, window_end_ms=end, bucket_seconds=60, final=True,
        event_count=sum(r['body']['event_count'] for r in current), metrics=metrics,
        source_refs=_references(current), baseline=baseline, reader_snapshot=audience,
        reader_context={'manifest': {key: session['manifest'][key] for key in ('identity_sha256', 'catalog')}})
    body['event_digest'] = digest([r['body']['event_digest'] for r in current])
    if discovery_policy is not None:
        from app.modules.hot_news.scoring.discovery import policy_for
        body['discovery_policy'] = policy_for(discovery_policy).manifest()
        body['run_id'] = 'window-' + digest(dict(session=session['id'], start=start, minutes=minutes,
            discovery_policy=body['discovery_policy']))[:32]
    validate_composition(body)
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return text, sha256(text.encode()).hexdigest()


def validate_composition(body):
    if 'discovery_policy' in body:
        from app.modules.hot_news.scoring.discovery import validate_policy
        validate_policy(body['discovery_policy'])
    start, end = body['window_start_ms'], body['window_end_ms']
    if (end - start not in (5 * MINUTE, 15 * MINUTE) or start % MINUTE
            or body['final'] is not True or body['bucket_seconds'] != 60):
        raise ValueError('Invalid composed window')
    def refs(values, left, right):
        if [r['window_start_ms'] for r in values] != list(range(left, right, MINUTE)):
            raise ValueError('Source minutes are not complete and ordered')
        if len({r['snapshot_id'] for r in values}) != len(values):
            raise ValueError('Duplicate source snapshot')
        for r in values:
            if (not re.fullmatch('[0-9a-f]{64}', r['snapshot_id'])
                    or type(r['revision']) is not int or r['revision'] < 1
                    or type(r['reader_revision']) is not int or r['reader_revision'] < 1):
                raise ValueError('Invalid source revision')
    refs(body['source_refs'], start, end)
    base = body['baseline']
    if (base['policy'] != BASELINE_POLICY or base['window_end_ms'] != start
            or base['window_start_ms'] != start - (end - start)):
        raise ValueError('Baseline must be the preceding equal-width window')
    if base['status'] == 'complete':
        refs(base['sources'], base['window_start_ms'], start)
    elif base['status'] != 'unavailable_before_session' or base['sources'] or base['metrics']:
        raise ValueError('Invalid missing baseline')
    reader = body['reader_snapshot']
    if (reader['tenant_id'] != body['tenant_id'] or reader['start_ms'] != start or reader['end_ms'] != end
            or reader['identity_sha256'] != digest({k: v for k, v in reader.items() if k != 'identity_sha256'})
            or not end <= reader['watermark_ms']
            or not _utc(reader['watermark_ms']) <= datetime.fromisoformat(reader['generated_at']) <= datetime.now(timezone.utc)):
        raise ValueError('Reader snapshot differs from the composed window')
    items = [ReaderProfileSummary.model_validate(r) for r in reader['items']]
    if ({r.news_id for r in items} != {r['news_id'] for r in body['metrics']}
            or len(items) != len(body['metrics']) or any(r.profile_cutoff != _utc(start) for r in items)):
        raise ValueError('Reader population or feature cutoff differs from the window')
    for group in (body['metrics'], base['metrics']):
        if len({r['news_id'] for r in group}) != len(group):
            raise ValueError('Duplicate metric identity')
        for r in group:
            ContentType(r['content_type'])
            if any(type(r[k]) is not int or r[k] < 0 for k in FIELDS):
                raise ValueError('Invalid aggregate metric')
            if not r['unique_users'] <= r['clicks'] <= r['impressions'] or r['effective_consumptions'] > r['clicks']:
                raise ValueError('Invalid aggregate funnel')


class WindowBaselineProvider:
    def __init__(self, body, bundle):
        self.body, self.bundle = body, bundle

    async def get_baselines(self, *, tenant_id, window_start, window_end, production_bundle_version, metric_keys):
        b = self.body
        if (tenant_id != b['tenant_id'] or window_start != _utc(b['window_start_ms'])
                or window_end != _utc(b['window_end_ms']) or production_bundle_version != self.bundle
                or not metric_keys <= {(r['news_id'], ContentType(r['content_type'])) for r in b['metrics']}):
            raise ValueError('Baseline query crossed frozen window scope')
        base = b['baseline']
        return {(r['news_id'], ContentType(r['content_type'])): NewsMetricBaseline(
            news_id=r['news_id'], content_type=ContentType(r['content_type']),
            sample_count=1, **{k: Decimal(r[k]) for k in FIELDS},
            ctr=Decimal(r['clicks']) / r['impressions'] if r['impressions'] else Decimal(0))
            for r in base['metrics'] if (r['news_id'], ContentType(r['content_type'])) in metric_keys}


def publish_window(db, *, tenant_id, session_id, window_start_ms, window_minutes, discovery_policy=None):
    """Freeze every input and enqueue exactly one immutable composite per identity."""
    from psycopg.errors import SerializationFailure
    for attempt in range(2):
        try:
            return _publish_window(db,tenant_id=tenant_id,session_id=session_id,
                window_start_ms=window_start_ms,window_minutes=window_minutes,discovery_policy=discovery_policy)
        except SerializationFailure:
            if attempt:
                raise WindowUnavailable('Source changed concurrently; retry this window query') from None


def _publish_window(db, *, tenant_id, session_id, window_start_ms, window_minutes, discovery_policy=None):
    validate_request(tenant_id, session_id, window_start_ms, window_minutes)
    start, end = window_start_ms, window_start_ms + window_minutes * MINUTE
    # The live consumer retains 31 days of interest history. A 30-day feature
    # lookback is complete only for query starts within the preceding day.
    # Older already-published reports remain readable by their snapshot ID.
    if start < int(datetime.now(timezone.utc).timestamp()*1000)-86_400_000:
        raise WindowUnavailable('Window exceeds the retained complete reader-history range (24 hours)')
    with db.transaction():
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        db.execute("SET LOCAL statement_timeout='10s'")
        # Conflicting publishers serialize; if MVCC began before a competing
        # commit, the bounded outer retry takes a fresh transaction snapshot.
        db.execute('SELECT pg_advisory_xact_lock(%s)', (int(digest(dict(tenant=tenant_id, session=session_id, start=start, end=end))[:15], 16),))
        session = db.execute('SELECT * FROM stream.wallclock_session WHERE id=%s', (session_id,)).fetchone()
        if session is None:
            raise WindowUnavailable('Unknown synthetic session')
        catalog = load_catalog(db, session['catalog_id'])
        left = max(session['start_ms'], start - window_minutes * MINUTE)
        keys = [f'{tenant_id}/{run_id(session, stamp)}/{stamp}' for stamp in range(left, end, MINUTE)]
        rows = db.execute('''SELECT s.*, w.final AS reader_final, w.event_count AS reader_event_count,
            w.revision AS reader_revision,w.watermark_ms AS reader_watermark,w.generated_at AS reader_generated_at,
            EXISTS(SELECT 1 FROM stream.invalid_snapshots i WHERE i.snapshot_id=s.snapshot_id) AS invalidated
            FROM stream.window_heads h JOIN stream.snapshots s ON s.snapshot_id=h.snapshot_id
            LEFT JOIN stream.reader_window w ON w.window_key=h.window_key
            WHERE h.window_key=ANY(%s) ORDER BY s.window_start_ms''', (keys,)).fetchall()
        clicks = db.execute('''SELECT window_ms,news_id,user_id FROM stream.reader_click
            WHERE session_id=%s AND tenant_id=%s AND window_ms>=%s AND window_ms<%s''',
            (session_id, tenant_id, left, end)).fetchall()
        visits = db.execute('''SELECT tenant_id,window_ms,news_id,user_id FROM stream.reader_visit
            WHERE session_id=%s AND tenant_id=%s AND window_ms>=%s AND window_ms<%s''',
            (session_id, tenant_id, start, end)).fetchall()
        history = db.execute('''SELECT tenant_id,user_id,news_id,minute_ms,weighted_score FROM stream.synthetic_reader_history
            WHERE session_id=%s AND tenant_id=%s AND minute_ms>=%s AND minute_ms<%s''',
            (session_id, tenant_id, start - 720 * 3_600_000, start)).fetchall()
        text, sid = compose(session, catalog, tenant_id, start, window_minutes, rows, clicks, visits, history,
                            discovery_policy=discovery_policy)
        body = json.loads(text)
        existing = db.execute('SELECT s.revision,l.state FROM stream.snapshots s JOIN stream.consumption_ledger l USING(snapshot_id) WHERE s.snapshot_id=%s', (sid,)).fetchone()
        if existing:
            return dict(snapshot_id=sid, run_id=body['run_id'], revision=existing['revision'], status=existing['state'], replayed=True)
        key = f'{tenant_id}/{body["run_id"]}/{start}'
        db.execute('INSERT INTO stream.window_heads(window_key) VALUES(%s) ON CONFLICT DO NOTHING', (key,))
        head = db.execute('SELECT revision FROM stream.window_heads WHERE window_key=%s FOR UPDATE', (key,)).fetchone()
        revision = head['revision'] + 1
        db.execute('''INSERT INTO stream.snapshots(snapshot_id,window_key,revision,tenant_id,run_id,
            window_start_ms,window_end_ms,final,payload_text) VALUES(%s,%s,%s,%s,%s,%s,%s,true,%s)''',
            (sid, key, revision, tenant_id, body['run_id'], start, end, text))
        for ref in body['source_refs'] + body['baseline']['sources']:
            db.execute('INSERT INTO stream.window_dependencies VALUES(%s,%s)', (sid, ref['snapshot_id']))
        db.execute('INSERT INTO stream.consumption_ledger(snapshot_id,workflow_id) VALUES(%s,%s)', (sid, 'stream-snapshot-' + sid))
        db.execute('''UPDATE stream.window_heads SET revision=%s,event_count=%s,event_digest=%s,
            final=true,snapshot_id=%s WHERE window_key=%s''', (revision, body['event_count'], body['event_digest'], sid, key))
        return dict(snapshot_id=sid, run_id=body['run_id'], revision=revision, status='pending', replayed=False)
