"""Project committed monitor reports, never replay historic news as live notices."""
from datetime import datetime, timezone

from app.modules.hot_news.contracts.monitor import HotNewsNotice, MonitorFeed, MonitorView
from app.modules.hot_news.scoring.discovery import VERSION, validate_policy
from simulation.streaming_lab.monitor import register, set_enabled
from simulation.streaming_lab.public_news import digest
from simulation.streaming_lab.window_query import MINUTE

TTL_MS = 5 * MINUTE

def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def start_latest(db, tenant_id, window_minutes):
    # Source collection is a separate explicit operation; this never starts a feed.
    with db.transaction():
        db.execute("SELECT pg_advisory_xact_lock(%s)", (int(digest(dict(tenant=tenant_id, minutes=window_minutes))[:15], 16),))
        session = db.execute("""SELECT s.* FROM stream.wallclock_session s
            JOIN stream.public_catalog c USING(catalog_id)
            WHERE s.enabled AND c.manifest->'tenants' ? %s
            ORDER BY s.start_ms DESC,s.id LIMIT 1""", (tenant_id,)).fetchone()
        if not session:
            raise ValueError("No active source session")
        previous = db.execute("""SELECT * FROM stream.hot_monitors
            WHERE tenant_id=%s AND session_id=%s AND window_minutes=%s AND policy_version=%s""",
            (tenant_id, session['id'], window_minutes, VERSION)).fetchone()
        if previous:
            return set_enabled(db, previous['monitor_id'], True)
        start = max(session['start_ms'], now_ms() // MINUTE * MINUTE - window_minutes * MINUTE)
        return register(db, tenant_id=tenant_id, session_id=session['id'],
                        window_start_ms=start, window_minutes=window_minutes)

def switch_owned(db, tenant_id, monitor_id, enabled):
    # Identity is checked before the existing cursor/publication lock.
    row = db.execute("SELECT 1 FROM stream.hot_monitors WHERE tenant_id=%s AND monitor_id=%s",
                     (tenant_id, monitor_id)).fetchone()
    return set_enabled(db, monitor_id, enabled) if row else None

def project_notices(rows, tenant_id, timestamp):
    """One latest *completed* window per monitor, then one card per news ID.

    An empty or invalid latest report removes the preceding report; pending newer
    analyses may keep a still-fresh completed report with its original timestamp.
    """
    by_news = {}
    for row in rows:
        end = row.get('window_end_ms')
        if (not row['enabled'] or not row['source_enabled'] or not row.get('final')
                or row.get('invalidated') or end is None or not end <= timestamp < end + TTL_MS):
            continue
        report = row.get('report') or {}
        if (report.get('tenant_id') != tenant_id or report.get('snapshot_id') != row['snapshot_id']
                or report.get('window_start_ms') != row['window_start_ms']
                or report.get('window_end_ms') != end
                or not report.get('final') or report.get('source_mode') != 'synthetic'
                or report.get('model_mode') != 'deterministic_local_port'
                or report.get('event_time_mode') != 'wall_clock'):
            continue
        discovery = report.get('discovery', {})
        try:
            validate_policy(discovery.get('policy', {}))
        except ValueError:
            continue
        candidates = {c['news_id']: c for c in discovery.get('candidates', []) if c.get('qualifies') and c.get('selected')}
        analyses = {a['news_id']: a for a in report.get('analyses', [])}
        for item in report.get('ranking', []):
            news_id = item['news_id']
            analysis, candidate = analyses.get(news_id), candidates.get(news_id)
            # Older reports without a frozen title are not silently re-labelled.
            if not analysis or not analysis.get('title') or not candidate:
                continue
            card = HotNewsNotice(
                notice_id=digest(dict(tenant_id=tenant_id, news_id=news_id)),
                monitor_id=row['monitor_id'], snapshot_id=row['snapshot_id'], news_id=news_id,
                title=analysis['title'], window_start_ms=row['window_start_ms'],
                window_end_ms=end, expires_at_ms=end + TTL_MS, hot_score=item['hot_score'],
                clicks=item['metrics']['clicks'], unique_users=item['metrics']['unique_users'],
                trend_assessment=analysis['report']['trend_assessment'])
            old = by_news.get(news_id)
            if old is None or (card.window_end_ms, card.window_start_ms, card.monitor_id) > (
                    old.window_end_ms, old.window_start_ms, old.monitor_id):
                by_news[news_id] = card
    return sorted(by_news.values(), key=lambda c: (-c.window_end_ms, -c.hot_score, c.news_id))[:3]

def feed(db, tenant_id, *, timestamp=None):
    timestamp = now_ms() if timestamp is None else timestamp
    with db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        monitors = db.execute("""SELECT m.*,s.enabled AS source_enabled FROM stream.hot_monitors m
            JOIN stream.wallclock_session s ON s.id=m.session_id
            WHERE m.tenant_id=%s ORDER BY m.updated_at DESC,m.monitor_id LIMIT 100""", (tenant_id,)).fetchall()
        rows = []
        for m in monitors:
            if not m['enabled']:
                continue
            # Select the latest completed report before checking validity: no stale fallback.
            row = db.execute("""SELECT s.*,r.report,
                EXISTS(SELECT 1 FROM stream.invalid_snapshots i WHERE i.snapshot_id=s.snapshot_id) AS invalidated
                FROM stream.hot_monitor_windows w JOIN stream.snapshots s USING(snapshot_id)
                JOIN stream.reports r USING(snapshot_id)
                JOIN stream.consumption_ledger l USING(snapshot_id)
                WHERE w.monitor_id=%s AND s.tenant_id=%s AND l.state='completed'
                ORDER BY s.window_end_ms DESC,s.revision DESC LIMIT 1""",
                (m['monitor_id'],tenant_id)).fetchone()
            if row:
                rows.append({**row, **{k:m[k] for k in ('monitor_id','enabled','source_enabled')}})
        return MonitorFeed(tenant_id=tenant_id,
            source_state='ready' if any(m['enabled'] and m['source_enabled'] for m in monitors) else 'waiting_source',
            monitors=[MonitorView(**{k:m[k] for k in MonitorView.model_fields}) for m in monitors],
            items=project_notices(rows,tenant_id,timestamp))

