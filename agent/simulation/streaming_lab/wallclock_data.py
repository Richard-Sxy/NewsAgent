"""Deterministic synthetic sessions, scheduled against real UTC, never future markers."""
from functools import lru_cache
from hashlib import sha256
import random

from simulation.streaming_lab.dynamic_data import TENANTS

VERSION = 'wallclock-sessions-readers-v1'
TOPIC = 'newsagent-wallclock-v1'
HALF_LIFE_MS = 3*86400000


def preferred(user, roots):
    number = int(user.rsplit('-',1)[1])
    return roots[number % len(roots)]


def run_id(session, start):
    return f"wc-{session['id']}-{start//60000}"


def packet(session, start):
    """Stable retry bytes. 70% interested returning visitors, plus new/unknown people.

    Small delivery jitter, whole-session late arrivals and duplicate delivery are
    deliberate. They do not change the underlying event timestamp or identity.
    """
    manifest = session['manifest']
    if start < session['start_ms']:
        return []
    rows = sorted((r for r in manifest['news'].values() if r['publish_ms']<=start), key=lambda r:r['news_id'])
    minute = (start-session['start_ms'])//60000
    # Two-minute overlap: the same articles keep accumulating returning readers.
    offset = (minute//2) % len(rows)
    selected = [rows[(offset+i*max(1,len(rows)//3))%len(rows)] for i in range(3)]
    rng = random.Random(f'{VERSION}:{session["id"]}:{start}')
    roots = manifest['roots']
    events = []
    for ti, tenant in enumerate(TENANTS):
        for ni, news in enumerate(selected):
            affinity = [i for i in range(80) if preferred(f'synthetic-user-{i}',roots) in news['topic_ids']]
            sessions = 12 + (minute+ni*3+ti)%12
            for si in range(sessions):
                user = rng.choice(affinity) if affinity and rng.random()<.7 else rng.randrange(100)
                user = f'synthetic-user-{user}'  # Identical strings across tenants; keys always tenant scoped.
                stamp = start+rng.randrange(1,35)*1000+rng.randrange(600)
                duration = rng.randrange(8,24)
                delay = rng.randrange(0,1800)
                if si==0:  # One late, valid visit per article/window.
                    stamp = start+55000
                    duration = 1
                    delay = 16000+ni*700
                sequence = [('impression',0,0)]
                if si%5!=4:  # Exposure does not always become a visit.
                    sequence += [('click',80,0),('detail_view',160,0),
                                 ('read' if news['content_type']=='article' else 'play',duration*1000,duration)]
                    if si%4==0:
                        sequence += [('like',duration*1000+100,0)]
                for ei,(kind,delta,seconds) in enumerate(sequence):
                    event = dict(schema_version=2,synthetic=True,kind='event',catalog_id=session['catalog_id'],
                        event_id=f'{run_id(session,start)}:{start}:{ti}-{ni}-{si}-{ei}',
                        run_id=run_id(session,start),tenant_id=tenant,event_time_ms=stamp+delta,
                        news_id=news['news_id'],content_type=news['content_type'],user_id=user,
                        event_type=kind,duration_seconds=seconds)
                    events.append((stamp+delta+delay,event))
                    if (si+ei)%17==0:
                        events.append((stamp+delta+delay+1800,event))
    return sorted(events,key=lambda row:(row[0],row[1]['event_id']))


def due(session, second, packets):
    start = second*1000//60000*60000
    result = []
    for window in (start-60000,start):
        if window not in packets:
            packets[window] = packet(session,window)
        result += [event for delivery,event in packets[window] if delivery//1000==second]
    for old in list(packets):
        if old<start-60000:
            del packets[old]
    # Heartbeat follows scheduled events; it advances only to that real second.
    result.append(dict(schema_version=2,synthetic=True,kind='watermark',catalog_id=session['catalog_id'],
        tenant_id=TENANTS[0],run_id=run_id(session,start),event_time_ms=second*1000))
    return result


def fingerprint(event):
    fields = ('catalog_id','news_id','content_type','event_type','user_id','event_time_ms','duration_seconds')
    return sha256('|'.join(str(event[k]) for k in fields).encode()).hexdigest()


def seed_history(session):
    """Explicit simulated warm-start, with publication dates respected; not observed history."""
    roots = session['manifest']['roots']
    rows = sorted(session['manifest']['news'].values(),key=lambda r:r['news_id'])
    for tenant in TENANTS:
        for user in range(80):
            uid = f'synthetic-user-{user}'
            root = preferred(uid,roots)
            for age in (.1,.5,2,5,10,16,23,29):
                stamp = (session['start_ms']-int(age*86400000))//60000*60000
                eligible = [r for r in rows if r['publish_ms']<=stamp and root in r['topic_ids']]
                if eligible:
                    index = int(sha256(f'{tenant}:{user}:{age}'.encode()).hexdigest()[:8],16)%len(eligible)
                    yield (session['id'],tenant,uid,eligible[index]['news_id'],stamp,2.0)
