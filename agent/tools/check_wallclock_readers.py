"""Read-only live latency samples and independent final-window metric/audience oracle."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
import json
import time

from simulation.streaming_lab.dynamic_feed import connect
from simulation.streaming_lab.public_news import connect_target
from simulation.streaming_lab.wallclock_data import packet,due,TENANTS,HALF_LIFE_MS
from tools.streaming_lab_acceptance import recount


def ratio(n,d):
    return (Decimal(n)/d).quantize(Decimal('.00000001'),rounding=ROUND_HALF_EVEN) if d else Decimal(0)


def verify_final(source,target,session,window):
    events=[e for _,e in packet(session,window['start_ms'])]
    expected,count=recount(events,window['tenant_id'])
    live=target.execute('SELECT * FROM news_realtime.live_window WHERE window_key=%s',(window['window_key'],)).fetchone()
    assert live and live['final'] and live['metrics']==expected and live['event_count']==count
    final=target.execute('SELECT * FROM news_realtime.reader_final WHERE window_key=%s',(window['window_key'],)).fetchone()
    assert final and final['items']==live['readers']
    readers=defaultdict(set)
    for e in events:
        if e['tenant_id']==window['tenant_id'] and e['event_type']=='detail_view':
            readers[e['news_id']].add(e['user_id'])
    history=source.execute('''SELECT user_id,news_id,minute_ms,weighted_score FROM stream.synthetic_reader_history
        WHERE session_id=%s AND tenant_id=%s AND minute_ms<%s AND minute_ms>=%s''',
        (session['id'],window['tenant_id'],window['start_ms'],window['start_ms']-720*3600000)).fetchall()
    checked=0
    for item in final['items']:
        news=item['news_id'];visitors=readers[news]
        assert item['reader_count']==len(visitors)
        for name,hours in [('short_term',24),('long_term',720)]:
            scores=defaultdict(lambda:defaultdict(float));evidence=set()
            for r in history:
                if r['news_id']==news or r['minute_ms']<window['start_ms']-hours*3600000:
                    continue
                uid=r['user_id']
                if int(uid.rsplit('-',1)[1])>=90:
                    continue
                evidence.add(uid)
                weight=r['weighted_score']*2**((r['minute_ms']-window['start_ms'])/HALF_LIFE_MS)
                for topic in session['manifest']['news'][r['news_id']]['topic_ids']:
                    scores[uid][topic]+=weight
            members={uid:{t for t,w in values.items() if w>1e-9 and w/max(values.values())>=.2-1e-12}
                     for uid,values in scores.items() if values and max(values.values())>1e-9}
            profiled={u for u,topics in members.items() if topics}
            counts=defaultdict(int);base=defaultdict(int)
            for uid,topics in members.items():
                for topic in topics:
                    base[topic]+=1
                    counts[topic]+=int(uid in visitors)
            dim=item[name]
            assert dim['profiled_reader_count']==len(visitors&profiled)
            assert dim['unknown_reader_count']==len(visitors-profiled)
            assert dim['cold_start_reader_count']==sum(int(u.rsplit('-',1)[1])<90 and u not in evidence for u in visitors)
            assert dim['baseline_profiled_count']==len(profiled)
            ordered=sorted(((t,n) for t,n in counts.items() if n),key=lambda v:(-v[1],v[0]))[:20]
            assert [(v['topic_id'],v['reader_count']) for v in dim['topics']]==ordered
            for topic in dim['topics']:
                n=topic['reader_count'];b=base[topic['topic_id']]
                assert topic['baseline_reader_count']==b
                assert Decimal(topic['share_all'])==ratio(n,len(visitors))
                assert Decimal(topic['share_profiled'])==ratio(n,len(visitors&profiled))
                assert Decimal(topic['baseline_share'])==ratio(b,len(profiled))
                low=min(n,b,len(visitors&profiled),len(profiled))<5
                assert topic['low_sample']==low
                lift=(ratio(n,len(visitors&profiled))/ratio(b,len(profiled))).quantize(Decimal('.00000001'),rounding=ROUND_HALF_EVEN) if not low and b else None
                assert (Decimal(topic['lift']) if topic['lift'] is not None else None)==lift
            checked+=1
    report=target.execute('''SELECT snapshot_id,report,projected_at FROM news_realtime.snapshot
        WHERE window_key=%s ORDER BY revision DESC LIMIT 1''',(window['window_key'],)).fetchone()
    assert report and report['report']['reader_profile_status']=='complete'
    bundle=report['report']['production_bundle_version']
    assert bundle in ('streaming-public-readers-bundle-v1','streaming-public-readers-retrieval-v2')
    if bundle=='streaming-public-readers-retrieval-v2':
        retrieval=report['report']['retrieval']
        assert retrieval['tenant_id']==window['tenant_id']
        assert retrieval['as_of_ms']==window['start_ms']+60000
        assert retrieval['calls'] and retrieval['policy_version']=='streaming-public-sql-lexical-v1'
    assert report['report']['external_model_calls']==0
    return dict(window_key=window['window_key'],news=len(final['items']),dimensions_checked=checked,
        unique_events=count,snapshot_id=report['snapshot_id'],trace_id=report['report']['trace_id'],
        window_end_to_report_sql_seconds=report['projected_at'].timestamp()-(window['start_ms']+60000)/1000)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe-seconds',type=int,default=90)
    args=parser.parse_args()
    if not 0<=args.observe_seconds<=180:
        raise ValueError('Observation bounded to three minutes')
    samples=[];seen={};packets={};updates=set();positions={};prefix_checks=0
    with connect() as source, connect_target() as target:
        target.autocommit=True
        session=source.execute('SELECT * FROM stream.wallclock_session ORDER BY start_ms DESC LIMIT 1').fetchone()
        deadline=time.monotonic()+args.observe_seconds
        while time.monotonic()<deadline:
            rows=target.execute('SELECT * FROM news_realtime.live_window WHERE session_id=%s AND start_ms>%s',
                (session['id'],int(time.time()*1000)-120000)).fetchall()
            observed=time.time()
            for row in rows:
                if row['final']:
                    continue
                stamp=row['start_ms']
                if stamp not in packets:
                    packets[stamp]=packet(session,stamp)
                if row.get('source_sequence'):
                    if stamp not in positions:
                        cache={};positions[stamp]=[]
                        for second in range(stamp//1000,stamp//1000+80):
                            for index,e in enumerate(due(session,second,cache)):
                                if e['kind']=='event' and e['event_time_ms']//60000*60000==stamp:
                                    positions[stamp].append((second*10000+index,e))
                    prefix=[e for sequence,e in positions[stamp] if sequence<=row['source_sequence']]
                    expected,count=recount(prefix,row['tenant_id'])
                    assert row['metrics']==expected and row['event_count']==count,'Live Flink prefix differs'
                    for summary in row['readers']:
                        visitors={e['user_id'] for e in prefix if e['tenant_id']==row['tenant_id']
                            and e['news_id']==summary['news_id'] and e['event_type']=='detail_view'}
                        assert summary['reader_count']==len(visitors),'Live reader prefix differs'
                    prefix_checks+=1
                for metric in row['metrics']:
                    key=(row['window_key'],metric['news_id'])
                    before=seen.get(key);seen[key]=metric['impressions']
                    if before is None or before>=metric['impressions']:
                        continue
                    updates.add((row['window_key'],row['revision']))
                    unique={}
                    for delivery,e in packets[stamp]:
                        if e['tenant_id']==row['tenant_id'] and e['news_id']==metric['news_id'] and e['event_type']=='impression':
                            unique.setdefault(e['event_id'],(delivery,e['event_time_ms']))
                    arrivals=sorted(unique.values())
                    for delivery,event_ms in arrivals[before:metric['impressions']]:
                        samples.append(dict(seconds=round(observed-event_ms/1000,3),late=delivery>=stamp+60000))
            time.sleep(1)
        windows=source.execute('''SELECT * FROM stream.reader_window w WHERE session_id=%s AND final AND delivered
            AND generated_at<clock_timestamp()-interval '10 seconds'
            AND NOT EXISTS(SELECT 1 FROM stream.invalid_windows i WHERE i.window_key=w.window_key)
            ORDER BY start_ms DESC,tenant_id LIMIT 4''',(session['id'],)).fetchall()
        assert len(windows)==4,'Need two complete tenant windows'
        checks=[verify_final(source,target,session,w) for w in windows]
        def stats(rows):
            values=sorted(r['seconds'] for r in rows)
            return dict(count=len(values),p50=values[len(values)//2],p95=values[min(len(values)-1,int(len(values)*.95))],maximum=max(values)) if values else None
        result=dict(status='passed',observed_at=datetime.now(timezone.utc).isoformat(),session_id=session['id'],
            source_mode='synthetic',event_time_mode='wall_clock',observation_seconds=args.observe_seconds,
            latency_definition='First SQL polling observation minus impression event time; polling adds up to one second.',
            normal_event_to_sql_seconds=stats([r for r in samples if not r['late']]),
            intentionally_late_event_to_sql_seconds=stats([r for r in samples if r['late']]),
            distinct_live_revisions_observed=len(updates),aligned_live_prefix_checks=prefix_checks,final_checks=checks)
        if args.observe_seconds:
            assert result['normal_event_to_sql_seconds'] and len(updates)>=4
            assert result['normal_event_to_sql_seconds']['p95']<15
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
