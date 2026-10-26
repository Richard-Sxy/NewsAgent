"""Versioned fictional traffic; pure deterministic generation, never model-generated numbers."""
from hashlib import sha256
import json
import random

VERSION = 'dynamic-traffic-v2'
TENANTS = ('11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222')
NEWS = (('synthetic-article-1', 'article'), ('synthetic-video-2', 'video'), ('synthetic-article-3', 'article'))
PHASES = ('warming', 'rising', 'breaking', 'peak', 'cooling', 'long_tail')
INTENSITY = (12, 24, 52, 70, 32, 14)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def packet(sequence, start, version=VERSION):
    if type(sequence) is not int or sequence < 1 or type(start) is not int or start % 60000:
        raise ValueError('Invalid synthetic clock/sequence')
    if version not in ('dynamic-traffic-v1', VERSION):
        raise ValueError('Unknown dynamic traffic version')
    run = f'live-{sequence:08d}'
    randomizer = random.Random(f'{version}:{sequence}:{start}')
    events = []
    for t, tenant in enumerate(TENANTS):
        for n, (news, content) in enumerate(NEWS):
            phase = (sequence - 1 + n * 2 + t) % len(PHASES)
            clicks = INTENSITY[phase] + randomizer.randrange(5)
            impressions = clicks * ((3 + (n + t) % 3) if version == 'dynamic-traffic-v1' else (10,7,4,2,5,9)[phase])
            # Returning readers generate several clicks; UV is never added across windows.
            audience = max(3, clicks * (50 + n * 8) // 100)
            reading = clicks * 3 // 4 if version == 'dynamic-traffic-v1' else clicks * (40,55,75,90,60,35)[phase] // 100
            likes = clicks // (5 if version == 'dynamic-traffic-v1' else (12,9,5,3,7,14)[phase])
            counts = [('impression', impressions), ('click', clicks),
                      ('read' if content == 'article' else 'play', reading),
                      ('like', likes), ('share', clicks // 12)]
            for kind, count in counts:
                for i in range(count):
                    events.append(dict(schema_version=1, synthetic=True, kind='event', tenant_id=tenant,
                        run_id=run, event_id=f'{run}:{start}:{t}:{news}:{kind}:{i}',
                        event_time_ms=start + 1000 + randomizer.randrange(48000),
                        news_id=news, content_type=content, event_type=kind,
                        user_id=f'synthetic-user-{(t * 1000 + n * 100 + i % audience)}',
                        duration_seconds=randomizer.randrange(5, 66) if kind in ('read', 'play') else 0))
    randomizer.shuffle(events)
    # Exact retransmissions and bounded out-of-order delivery are deliberate fixtures.
    events += events[:12]
    marker = dict(schema_version=1, synthetic=True, kind='watermark', tenant_id=TENANTS[0],
                  run_id=run, event_time_ms=start + 94000)
    return dict(version=version, sequence=sequence, run_id=run, window_start_ms=start,
                scenario=PHASES[(sequence-1) % 6], events=events, marker=marker)


def digest(value):
    return sha256(encode(value)).hexdigest()


def enforce_budget(*, database_bytes, backlog, lag, failures):
    if database_bytes >= 512 * 1024**2:
        raise ValueError('SyntheticDatabaseBudget')
    if backlog > 40 or lag > 90 or failures:
        raise ValueError('SyntheticConsumerBudget')
