"""CLI-only synthetic full-window recount; never adds UV aggregates or reopens Flink state."""
from hashlib import sha256
import json
import re
import sys

from simulation.streaming_lab.bridge import CATALOG, FIELDS, validate_payload


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def recount(events, identity):
    if not isinstance(events, list) or not 0 < len(events) <= 20000:
        raise ValueError('Expected 1..20000 synthetic records')
    hashes, rows, users = {}, {}, {}
    for e in events:
        if not isinstance(e, dict):
            raise ValueError('Invalid synthetic event')
        stamp, duration = e.get('event_time_ms'), e.get('duration_seconds')
        news, kind, user, eid = (e.get(k) for k in ('news_id','event_type','user_id','event_id'))
        start = identity['window_start_ms']
        if (e.get('synthetic') is not True or type(e.get('schema_version')) is not int or e['schema_version'] != 1
                or e.get('kind') != 'event' or e.get('tenant_id') != identity['tenant_id']
                or e.get('run_id') != identity['run_id'] or type(stamp) is not int or not start <= stamp < start+60000
                or not isinstance(eid, str) or not re.fullmatch(r'[a-zA-Z0-9:_-]{1,160}', eid)
                or not eid.startswith(f"{identity['run_id']}:{start}:")
                or not isinstance(user, str) or not re.fullmatch(r'synthetic-user-[0-9]{1,5}', user)
                or not isinstance(news, str) or news not in CATALOG or e.get('content_type') != CATALOG[news][0]
                or kind not in ('impression','click','read','play','like','comment','share','favorite')
                or type(duration) is not int or not 0 <= duration <= 3600
                or kind == 'read' and e['content_type'] != 'article'
                or kind == 'play' and e['content_type'] != 'video'):
            raise ValueError('Event crossed synthetic tenant/window/contract boundary')
        content = e['content_type']
        fingerprint = sha256(f'{news}|{content}|{kind}|{user}|{stamp}|{duration}'.encode()).hexdigest()
        if eid in hashes:
            if hashes[eid] != fingerprint:
                raise ValueError('Conflicting event identity')
            continue
        hashes[eid] = fingerprint
        if len(hashes) > 10000:
            raise ValueError('Window event budget exceeded')
        row = rows.setdefault(news, dict(news_id=news,content_type=content,**dict.fromkeys(FIELDS,0)))
        if kind == 'impression': row['impressions'] += 1
        if kind == 'click':
            row['clicks'] += 1
            users.setdefault(news, set()).add(user)
        if kind in ('read','play'):
            row['total_duration_seconds'] += duration
            row['effective_consumptions'] += int(duration >= (20 if content == 'article' else 10))
        if kind in ('like','comment','share','favorite'): row['interactions'] += 1
    for news, row in rows.items(): row['unique_users'] = len(users.get(news, set()))
    return dict(tenant_id=identity['tenant_id'],run_id=identity['run_id'],window_start_ms=start,
                window_end_ms=start+60000,metric_version='synthetic-event-metrics-v1',final=True,
                event_count=len(hashes),event_digest=sha256(canonical(dict(sorted(hashes.items()))).encode()).hexdigest(),
                metrics=[rows[key] for key in sorted(rows)])


def prepare(package, base):
    body = validate_payload(base['payload_text'],base['snapshot_id'],package['tenant_id'])
    if not body['final']:
        raise ValueError('Backfill requires a finalized base snapshot')
    original = recount(package['base_events'],body)
    if original != body:
        raise ValueError('Full base events do not reproduce the published snapshot')
    # A full union, rather than summed UV, preserves overlap across old and late events.
    added = package['added_events']
    if not isinstance(added, list) or not 0 < len(added) <= 10000:
        raise ValueError('Expected bounded nonempty late events')
    updated = recount(package['base_events']+added,body)
    if updated['event_count'] <= body['event_count']:
        raise ValueError('Backfill contains no new event identities')
    payload = canonical(updated)
    sid = sha256(payload.encode()).hexdigest()
    validate_payload(payload,sid,package['tenant_id'])
    return payload, sid, updated, updated['event_count']-body['event_count']


def publish(db, package):
    if (not isinstance(package, dict) or type(package.get('schema_version')) is not int
            or package['schema_version'] != 1
            or not isinstance(package.get('request_id'), str) or not re.fullmatch('[a-z0-9-]{1,64}',package['request_id'])
            or not isinstance(package.get('base_snapshot_id'), str) or not re.fullmatch('[0-9a-f]{64}',package['base_snapshot_id'])
            or not isinstance(package.get('reason'), str) or not 1 <= len(package['reason'].strip()) <= 200
            or not {'tenant_id','base_events','added_events'} <= package.keys()):
        raise ValueError('Invalid backfill package')
    digest = sha256(json.dumps(package,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with db.transaction():
        base = db.execute('SELECT * FROM stream.snapshots WHERE snapshot_id=%s AND tenant_id=%s',
                          (package['base_snapshot_id'],package['tenant_id'])).fetchone()
        if not base: raise ValueError('Base snapshot not found for tenant')
        head = db.execute('SELECT * FROM stream.window_heads WHERE window_key=%s FOR UPDATE',(base['window_key'],)).fetchone()
        if db.execute('SELECT 1 FROM stream.invalid_windows WHERE window_key=%s',(base['window_key'],)).fetchone():
            raise ValueError('Invalidated window cannot be repaired by additive backfill')
        previous = db.execute('SELECT * FROM stream.backfills WHERE request_id=%s',(package['request_id'],)).fetchone()
        if previous:
            if previous['request_digest'] != digest:
                raise ValueError('Idempotency key reused with different package')
            return dict(snapshot_id=previous['snapshot_id'],base_snapshot_id=previous['base_snapshot_id'],replayed=True)
        if head['snapshot_id'] != base['snapshot_id'] or not head['final']:
            raise ValueError('Stale base snapshot; fetch the latest final revision')
        payload, sid, body, added_count = prepare(package,base)
        revision = head['revision']+1
        db.execute('INSERT INTO stream.snapshots(snapshot_id,window_key,revision,tenant_id,run_id,window_start_ms,window_end_ms,final,payload_text) VALUES(%s,%s,%s,%s,%s,%s,%s,true,%s)',
                   (sid,base['window_key'],revision,base['tenant_id'],base['run_id'],base['window_start_ms'],base['window_end_ms'],payload))
        db.execute('INSERT INTO stream.consumption_ledger(snapshot_id,workflow_id) VALUES(%s,%s)',(sid,'stream-snapshot-'+sid))
        db.execute("INSERT INTO stream.backfills(request_id,request_digest,snapshot_id,base_snapshot_id,method_version,added_event_count,reason) VALUES(%s,%s,%s,%s,'synthetic-full-recount-v1',%s,%s)",
                   (package['request_id'],digest,sid,base['snapshot_id'],added_count,package['reason'].strip()))
        db.execute('UPDATE stream.window_heads SET revision=%s,event_count=%s,event_digest=%s,final=true,snapshot_id=%s WHERE window_key=%s',
                   (revision,body['event_count'],body['event_digest'],sid,base['window_key']))
        return dict(snapshot_id=sid,base_snapshot_id=base['snapshot_id'],revision=revision,replayed=False)


def main():
    # No shell interpolation of payloads; host CLI pipes bounded UTF-8 JSON to stdin.
    raw = sys.stdin.buffer.read(8*1024*1024+1)
    if len(raw) > 8*1024*1024: raise ValueError('Package exceeds 8 MiB')
    from simulation.streaming_lab.runtime import connect
    with connect() as db:
        result = publish(db,json.loads(raw))
    print(json.dumps(result))


if __name__ == '__main__': main()
