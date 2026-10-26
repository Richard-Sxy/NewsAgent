"""Bounded synthetic producer + INDEPENDENT recount + isolated recovery acceptance.

Host stdlib only. No .env, enterprise data, external model, or existing stack writes.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import random
import shutil
import subprocess
import time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
HTTP = build_opener(ProxyHandler({}))
TENANTS = ['11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222']
NEWS = [('synthetic-article-1','article'),('synthetic-video-2','video'),('synthetic-article-3','article')]
METRICS = ['impressions','clicks','unique_users','total_duration_seconds','effective_consumptions','interactions']


def get(url):
    with HTTP.open(url, timeout=5) as response:
        return json.load(response)


def generate(run_id, start):
    events=[]
    for t,tenant in enumerate(TENANTS):
        for news,content in NEWS:
            counts={'impression':40+t*10,'click':20+t*2,'read' if content=='article' else 'play':10,'like':5}
            for kind,count in counts.items():
                for i in range(count):
                    events.append({'schema_version':1,'synthetic':True,'kind':'event','tenant_id':tenant,'run_id':run_id,
                        'event_id':f'{run_id}:{start}:{news}:{kind}:{i}', 'event_time_ms':start+1000+(i*251)%45000,
                        'news_id':news,'content_type':content,'event_type':kind,'user_id':f'synthetic-user-{i%13}',
                        'duration_seconds':(30 if content=='article' else 15) if kind in {'read','play'} else 0})
    random.Random(20261022).shuffle(events)
    return events


def recount(events,tenant):
    """Batch reference never imports the Java accumulator or NewsAgent calculator."""
    seen={}
    rows={}
    users=defaultdict(set)
    for event in events:
        if event['tenant_id']!=tenant: continue
        key=event['event_id']
        if key in seen:
            if seen[key]!=event: raise AssertionError('Reference has conflicting duplicate')
            continue
        seen[key]=event
        news=event['news_id'];kind=event['event_type'];content=event['content_type']
        row=rows.setdefault(news,dict(news_id=news,content_type=content,**dict.fromkeys(METRICS,0)))
        if kind=='impression': row['impressions']+=1
        if kind=='click': row['clicks']+=1; users[news].add(event['user_id'])
        if kind in {'read','play'}:
            row['total_duration_seconds']+=event['duration_seconds']
            row['effective_consumptions']+=int(event['duration_seconds']>=(20 if content=='article' else 10))
        if kind in {'like','comment','share','favorite'}: row['interactions']+=1
    for news,row in rows.items(): row['unique_users']=len(users[news])
    return [rows[k] for k in sorted(rows)],len(seen)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',default='acceptance-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    parser.add_argument('--fault-recovery',action='store_true')
    args=parser.parse_args()
    import re
    if not re.fullmatch('[a-z0-9-]{1,50}',args.run_id): raise SystemExit('Invalid run ID')
    path=ROOT/'output'/'streaming-lab'/args.run_id
    path.mkdir(parents=True,exist_ok=False)
    evidence={'run_id':args.run_id,'started_at':datetime.now(timezone.utc).isoformat(),'commands':[], 'checks':{},'external_model_calls':0}
    deadline=time.monotonic()+420

    def save(): (path/'acceptance.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')

    def command(label,cmd,input_text=None,timeout=45):
        began=time.monotonic()
        remaining=deadline-began
        if remaining<=0:
            raise TimeoutError('Overall acceptance deadline')
        timeout=min(timeout,remaining)
        p=subprocess.run(cmd,input=input_text,encoding='utf-8',capture_output=True,timeout=timeout)
        evidence['commands'].append({'name':label,'command':cmd,'finished_at':datetime.now(timezone.utc).isoformat(),'duration_seconds':round(time.monotonic()-began,3),'exit_code':p.returncode,'stdout':p.stdout[-3000:],'stderr':p.stderr[-3000:]})
        save()
        if p.returncode: raise RuntimeError(label+' failed')
        return p.stdout

    def wait(label,read,predicate,timeout=60):
        limit=min(deadline,time.monotonic()+timeout)
        while time.monotonic()<limit:
            data=read()
            if predicate(data): print(label+': passed',flush=True); return data
            time.sleep(1)
        raise TimeoutError(label)

    def send(label,events):
        if len(events)>2500: raise ValueError('Batch event budget exceeded')
        lines=''.join(json.dumps(e,separators=(',',':'))+'\n' for e in events)
        evidence.setdefault('batches',[]).append({'name':label,'records':len(events),'sha256':sha256(lines.encode()).hexdigest()})
        command(label,['docker','exec','-i','newsagent-streaming-lab-kafka-1','/opt/kafka/bin/kafka-console-producer.sh','--bootstrap-server','kafka:9092','--topic','newsagent-synthetic-v1','--producer-property','acks=all','--producer-property','enable.idempotence=true','--producer-property','request.timeout.ms=10000','--producer-property','delivery.timeout.ms=20000'],lines)

    def marker(run_id,stamp):
        return {'schema_version':1,'synthetic':True,'kind':'watermark','tenant_id':TENANTS[0],'run_id':run_id,'event_time_ms':stamp}

    def read_run(run_id=args.run_id): return get('http://127.0.0.1:48000/runs/'+run_id)

    def ready_rows(data,final):
        rows=[r for r in data['snapshots'] if r['final']==final and r['state']=='completed' and not r['invalidated']]
        return {r['tenant_id'] for r in rows}==set(TENANTS)

    def check_rows(data,events,final):
        for tenant in TENANTS:
            row=max((r for r in data['snapshots'] if r['tenant_id']==tenant and r['final']==final),key=lambda r:r['revision'])
            expected,n=recount(events,tenant)
            assert row['snapshot']['metrics']==expected,(tenant,'aggregate mismatch')
            assert row['snapshot']['event_count']==n
            assert sorted(row['report']['ranking'],key=lambda r:r['news_id'])==sorted([
                {'rank':r['rank'],'news_id':r['news_id'],'hot_score':r['hot_score'],'metrics':{k:next(m for m in expected if m['news_id']==r['news_id'])[k] for k in METRICS}}
                for r in row['report']['ranking']],key=lambda r:r['news_id'])
            assert len(row['report']['analyses'])==3
            assert row['report']['external_model_calls']==0

    producer_lock=ROOT/'var/streaming-lab/producer.lock'
    with producer_lock.open('x',encoding='utf-8') as lock: lock.write(args.run_id)
    try:
        if shutil.disk_usage('D:\\').free<50*1024**3: raise RuntimeError('Disk stop condition')
        get('http://127.0.0.1:48000/ready')
        assert not read_run()['snapshots'],'Run ID already exists'
        job=next(j for j in get('http://127.0.0.1:48081/jobs/overview')['jobs'] if j['state']=='RUNNING')
        job_id=job['jid'];evidence['flink_job_id']=job_id
        state=ROOT/'var'/'streaming-lab'/'event-clock.json'
        last=json.loads(state.read_text())['last_start_ms'] if state.exists() else 0
        start=max(int(time.time()*1000)//60000*60000,last+180000)
        state.write_text(json.dumps({'last_start_ms':start,'run_id':args.run_id}),encoding='utf-8')
        evidence.update(window_start_ms=start,window_end_ms=start+60000,event_time_mode='accelerated synthetic clock',window_seconds=60,allowed_lateness_seconds=30)
        events=generate(args.run_id,start);first=events[:len(events)//2]
        send('first_half',first+first[:20]);sent_at=int(time.time()*1000)
        cp_url=f'http://127.0.0.1:48081/jobs/{job_id}/checkpoints'
        before=wait('checkpoint contains first batch',lambda:get(cp_url),lambda d:d.get('latest',{}).get('completed',{}).get('latest_ack_timestamp',0)>=sent_at)
        evidence['checkpoint_before_recovery']=before['latest']['completed']
        if args.fault_recovery:
            old_restored=before['counts']['restored']
            command('restart_own_taskmanager',['docker','restart','--time','10','newsagent-streaming-lab-taskmanager-1'],timeout=30)
            recovered=wait('Flink state restored',lambda:get(cp_url),lambda d:d['counts']['restored']>old_restored,timeout=100)
            evidence['checkpoint_restored']=recovered['latest']['restored']
            wait('job running',lambda:get(f'http://127.0.0.1:48081/jobs/{job_id}'),lambda d:d['state']=='RUNNING')
            evidence['checks']['taskmanager_checkpoint_recovery']='passed'
        else: evidence['checks']['taskmanager_checkpoint_recovery']='not_run'
        send('second_half_and_replayed_first',events[len(events)//2:]+first[:20])
        send('initial_watermark',[marker(args.run_id,start+64000)])
        initial=wait('initial two-tenant reports',read_run,lambda d:ready_rows(d,False))
        check_rows(initial,events,False);evidence['checks']['dedup_out_of_order_exact_uv_tenant_isolation']='passed'
        initial_ids={r['snapshot_id'] for r in initial['snapshots']}
        send('exact_duplicate_replay',events[:30]);time.sleep(3)
        assert {r['snapshot_id'] for r in read_run()['snapshots']}==initial_ids
        evidence['checks']['replay_no_new_snapshot']='passed'
        late=[]
        for tenant in TENANTS:
            template=next(e for e in events if e['tenant_id']==tenant and e['event_type']=='impression' and e['news_id']=='synthetic-article-1')
            for kind in ['impression','click']:
                late.append(dict(template,event_id=f'{args.run_id}:{start}:late-{kind}',event_type=kind,user_id='synthetic-user-99',event_time_ms=start+30000))
        send('allowed_late_correction',late)
        corrected=wait('late revision consumed',read_run,lambda d:all(any(r['tenant_id']==t and r['state']=='completed' and r['snapshot']['event_count']==recount(events+late,t)[1] for r in d['snapshots']) for t in TENANTS))
        check_rows(corrected,events+late,False)
        assert len({r['report']['run_key'] for r in corrected['snapshots'] if r['report']})==len([r for r in corrected['snapshots'] if r['report']])
        evidence['checks']['late_revision_identity_and_recompute']='passed'
        send('final_watermark',[marker(args.run_id,start+94000)])
        final=wait('final two-tenant reports',read_run,lambda d:ready_rows(d,True))
        check_rows(final,events+late,True)
        final_ids={r['snapshot_id'] for r in final['snapshots']}
        send('too_late', [dict(late[0],event_id=f'{args.run_id}:{start}:too-late')])
        closed=wait('too late rejected',read_run,lambda d:any(r['reason']=='too_late' for r in d['rejections']))
        assert {r['snapshot_id'] for r in closed['snapshots']}==final_ids
        evidence['checks']['closed_window_rejects_late_event']='passed'
        # Independent conflict window: no accepted snapshot may be published.
        conflict_id=args.run_id+'-conflict';conflict_start=start+120000
        one=generate(conflict_id,conflict_start)[0]
        send('conflicting_id',[one,dict(one,duration_seconds=one['duration_seconds']+1),marker(conflict_id,conflict_start+94000)])
        conflict=wait('conflict rejects window',lambda:read_run(conflict_id),lambda d:any(r['reason']=='conflicting_event_id' for r in d['rejections']))
        assert not conflict['snapshots']
        evidence['checks']['conflicting_event_id_fails_closed']='passed'
        state.write_text(json.dumps({'last_start_ms':conflict_start,'run_id':args.run_id}),encoding='utf-8')
        evidence['results']=closed;evidence['conflict_result']=conflict
        time.sleep(6)
        trace_ids=[r['report']['trace_id'] for r in closed['snapshots'] if r['final']]
        traces=[]
        for trace_id in trace_ids:
            trace=get('http://127.0.0.1:23200/api/traces/'+trace_id)
            (path/('trace-'+trace_id+'.json')).write_text(json.dumps(trace,ensure_ascii=False,indent=2),encoding='utf-8')
            traces.append({'trace_id':trace_id,'url':'http://127.0.0.1:23200/api/traces/'+trace_id})
        evidence['traces']=traces;evidence['checks']['final_reports_trace_retrieval']='passed'
        evidence['status']='passed'
    except Exception as exc:
        evidence['status']='failed';evidence['error_type']=type(exc).__name__;evidence['error']=str(exc)[:500]
        raise
    finally:
        evidence['finished_at']=datetime.now(timezone.utc).isoformat();save()
        if producer_lock.read_text(encoding='utf-8')==args.run_id: producer_lock.unlink()
        print(str(path/'acceptance.json'),flush=True)


if __name__=='__main__':main()
