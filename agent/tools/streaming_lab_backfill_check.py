"""Real SQL + Temporal + read API acceptance for finalized synthetic window backfill."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time

from streaming_lab_acceptance import generate, get, recount, TENANTS, ROOT
from streaming_lab_backfill import submit


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id')
    args=parser.parse_args()
    output=ROOT/'output'/'streaming-lab'/args.run_id/'backfill.json'
    if output.exists(): raise ValueError('Backfill evidence already exists; use a fresh acceptance run')
    evidence={'run_id':args.run_id,'started_at':datetime.now(timezone.utc).isoformat(),'checks':{}}
    deadline=time.monotonic()+180
    def read(): return get('http://127.0.0.1:48000/runs/'+args.run_id)
    def consume(sid):
        while time.monotonic()<deadline:
            row=next((r for r in read()['snapshots'] if r['snapshot_id']==sid),None)
            if row and row['state']=='completed': return row
            if row and row['state']=='failed': raise AssertionError('Backfill analysis failed')
            time.sleep(1)
        raise TimeoutError('Backfill consumption deadline')
    def reject(package,fragment):
        before={r['snapshot_id'] for r in read()['snapshots']}
        try: submit(package)
        except RuntimeError as exc:
            assert fragment in str(exc),str(exc)
        else: raise AssertionError('Invalid package was accepted')
        assert {r['snapshot_id'] for r in read()['snapshots']}==before
    try:
        initial=read()
        base=max((r for r in initial['snapshots'] if r['tenant_id']==TENANTS[0] and r['final']),key=lambda r:r['revision'])
        assert not base.get('base_snapshot_id'),'Use a new acceptance run'
        start=base['snapshot']['window_start_ms']
        originals=[e for e in generate(args.run_id,start) if e['tenant_id']==TENANTS[0]]
        template=next(e for e in originals if e['news_id']=='synthetic-article-1' and e['event_type']=='impression')
        for kind in ('impression','click'):
            originals.append(dict(template,event_id=f'{args.run_id}:{start}:late-{kind}',event_type=kind,user_id='synthetic-user-99',event_time_ms=start+30000))
        added=[]
        for n,user in enumerate(('synthetic-user-0','synthetic-user-100')):
            for kind in ('impression','click'):
                added.append(dict(template,event_id=f'{args.run_id}:{start}:backfill-{n}-{kind}',event_type=kind,user_id=user))
        added += [dict(template,event_id=f'{args.run_id}:{start}:backfill-{kind}',event_type=kind,duration_seconds=30 if kind=='read' else 0) for kind in ('read','like')]
        package=dict(schema_version=1,request_id=args.run_id+'-bf1',tenant_id=TENANTS[0],base_snapshot_id=base['snapshot_id'],
                     reason='合成超期事件补数：复核重叠用户与新增用户',base_events=originals,added_events=added+[added[1],originals[0]])
        wrong=deepcopy(package);wrong['tenant_id']=TENANTS[1]
        reject(wrong,'not found for tenant')
        wrong=deepcopy(package);wrong['base_events'][0]['user_id']='synthetic-user-101'
        reject(wrong,'do not reproduce')
        wrong=deepcopy(package);wrong['added_events']=[dict(originals[0],user_id='synthetic-user-102')]
        reject(wrong,'Conflicting')
        wrong=deepcopy(package);wrong['added_events'][0]['event_time_ms']=start+60000
        reject(wrong,'boundary')
        evidence['checks']['tenant_base_digest_event_conflict_window_guards']='passed'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(submit,[package,package]))
        assert results[0]['snapshot_id']==results[1]['snapshot_id']
        assert sorted(r['replayed'] for r in results)==[False,True]
        sid=results[0]['snapshot_id'];row=consume(sid)
        expected,count=recount(originals+added,TENANTS[0])
        assert row['snapshot']['metrics']==expected and row['snapshot']['event_count']==count
        old=next(r for r in base['snapshot']['metrics'] if r['news_id']=='synthetic-article-1')
        new=next(r for r in expected if r['news_id']=='synthetic-article-1')
        assert new['clicks']-old['clicks']==2 and new['unique_users']-old['unique_users']==1
        assert row['added_event_count']==6 and row['base_snapshot_id']==base['snapshot_id']
        assert row['report']['run_key']!=base['report']['run_key']
        evidence['checks']['concurrent_idempotency_full_recount_uv_overlap_new_analysis']='passed'
        wrong=deepcopy(package);wrong['reason']='changed'
        reject(wrong,'Idempotency key reused')
        wrong=deepcopy(package);wrong['request_id']=args.run_id+'-stale'
        reject(wrong,'Stale base')
        evidence['checks']['idempotency_conflict_and_stale_base']='passed'
        follow=deepcopy(package)
        follow.update(request_id=args.run_id+'-bf2',base_snapshot_id=sid,base_events=originals+added,
                      added_events=[dict(template,event_id=f'{args.run_id}:{start}:backfill-followup',event_type='like')],reason='第二次合成补数：验证修订连续性')
        result2=submit(follow);row2=consume(result2['snapshot_id'])
        replay=submit(package)
        assert replay['replayed'] and replay['snapshot_id']==sid
        after=read()
        assert len(after['snapshots'])==len(initial['snapshots'])+2
        for old_row in initial['snapshots']:
            now=next(r for r in after['snapshots'] if r['snapshot_id']==old_row['snapshot_id'])
            assert now['snapshot']==old_row['snapshot'] and now['report']==old_row['report']
        assert row2['revision']==row['revision']+1 and row2['base_snapshot_id']==sid
        filtered=get(f'http://127.0.0.1:48000/streaming-lab-api/runs/{args.run_id}?tenant_id={TENANTS[1]}')
        assert filtered['snapshots'] and all(r['tenant_id']==TENANTS[1] for r in filtered['snapshots'])
        assert all(r['tenant_id']==TENANTS[1] for r in filtered['rejections'])
        evidence['checks']['chained_revision_historical_replay_old_reports_unchanged_tenant_read']='passed'
        evidence['results']=after
        evidence['packages']=[{'request_id':p['request_id'],'base_snapshot_id':p['base_snapshot_id'],
            'sha256':sha256(json.dumps(p,ensure_ascii=False,sort_keys=True).encode()).hexdigest()} for p in (package,follow)]
        # Only synthetic events; retain reproducible CLI inputs beside acceptance evidence.
        for p in (package,follow):
            (output.parent/(p['request_id']+'.json')).write_text(json.dumps(p,ensure_ascii=False),encoding='utf-8')
        time.sleep(6)
        for r in (row,row2):
            trace=get('http://127.0.0.1:23200/api/traces/'+r['report']['trace_id'])
            (output.parent/('trace-'+r['report']['trace_id']+'.json')).write_text(json.dumps(trace,ensure_ascii=False),encoding='utf-8')
        evidence['checks']['backfill_analysis_traces']='passed'
        evidence['status']='passed'
    except Exception as exc:
        evidence.update(status='failed',error_type=type(exc).__name__,error=str(exc)[:500])
        raise
    finally:
        evidence['finished_at']=datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        print(str(output),flush=True)


if __name__=='__main__': main()
