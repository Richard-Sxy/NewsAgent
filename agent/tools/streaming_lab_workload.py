"""Finite multi-window synthetic workload with exact recount and resource stop rules.

Reuses the existing acceptance generator. Does not combine v8 hourly UV with raw
streaming events: they are different simulated worlds and remain separately tagged.
"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import random
import shutil
import time

from streaming_lab_acceptance import ROOT, generate, recount, get, TENANTS
from streaming_lab_ops import atomic_json, command


def resources():
    import ctypes
    class Memory(ctypes.Structure):
        _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(name,ctypes.c_ulonglong) for name in ('total','free','page_total','page_free','virtual_total','virtual_free','extended')]
    memory=Memory();memory.length=ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)): raise RuntimeError('Memory probe failed')
    return {'windows_free_gib':memory.free/1024**3,'d_free_gib':shutil.disk_usage('D:\\').free/1024**3}


def enforce_budget(sample,ops):
    if sample['windows_free_gib']<2 or sample['d_free_gib']<50:
        raise RuntimeError('Resource stop condition: memory or disk headroom')
    if ops['ledger']['pending']+ops['ledger']['started']>100 or ops['oldest_pending_seconds']>90:
        raise RuntimeError('Backlog stop condition')
    if ops['ledger']['failed'] or ops['dispatcher']['state']!='ready':
        raise RuntimeError('Consumer not healthy; stop producing')


def publish(events):
    if not 0<len(events)<=2500: raise ValueError('Producer batch budget')
    raw=''.join(json.dumps(e,separators=(',',':'))+'\n' for e in events).encode()
    command(['docker','exec','-i','newsagent-streaming-lab-kafka-1','/opt/kafka/bin/kafka-console-producer.sh',
             '--bootstrap-server','kafka:9092','--topic','newsagent-synthetic-v1','--producer-property','acks=all',
             '--producer-property','enable.idempotence=true','--producer-property','delivery.timeout.ms=20000',
             '--producer-property','request.timeout.ms=10000'],input=raw,timeout=30)
    return sha256(raw).hexdigest()


def allocate_clock(run_id,windows):
    path=ROOT/'var/streaming-lab/event-clock.json'
    last=json.loads(path.read_text())['last_start_ms'] if path.exists() else 0
    start=max(int(time.time()*1000)//60000*60000,last+180000)
    atomic_json(path,{'last_start_ms':start+(windows-1)*180000,'run_id':run_id})
    return start


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--windows',type=int,default=3)
    parser.add_argument('--rate',type=int,default=100,help='maximum requested records/second, not a throughput claim')
    args=parser.parse_args()
    if not 1<=args.windows<=6 or not 20<=args.rate<=300: raise ValueError('Use 1..6 windows and 20..300 records/second')
    run_id='workload-'+datetime.now().strftime('%Y%m%d-%H%M%S')
    output=ROOT/'output/streaming-lab'/run_id;output.mkdir(parents=True,exist_ok=False)
    evidence={'run_id':run_id,'started_at':datetime.now(timezone.utc).isoformat(),'profile':'synthetic-bounded-workload-v1',
              'requested_max_rate':args.rate,'event_time':'accelerated synthetic clock','windows':[],'resource_samples':[]}
    clock_path=ROOT/'var/streaming-lab/producer.lock'
    with clock_path.open('x') as lock: lock.write(run_id)
    deadline=time.monotonic()+360;began=time.monotonic();sent=0
    try:
        start=allocate_clock(run_id,args.windows)
        for number in range(args.windows):
            window=start+number*180000;scale=1+number%3
            original=generate(run_id,window)
            events=[dict(e,event_id=e['event_id']+f'-copy-{copy}') for copy in range(scale) for e in original]
            # Replicas intentionally share user IDs; UV must not scale with clicks.
            random.Random(20261022+number).shuffle(events)
            records=events+events[:30]
            for offset in range(0,len(records),args.rate):
                if time.monotonic()>deadline: raise TimeoutError('Workload deadline')
                sample=resources();ops=get('http://127.0.0.1:48000/streaming-lab-api/operations')
                enforce_budget(sample,ops);evidence['resource_samples'].append(sample)
                batch=records[offset:offset+args.rate];tick=time.monotonic();publish(batch);sent+=len(batch)
                time.sleep(max(0,len(batch)/args.rate-(time.monotonic()-tick)))
            publish([dict(schema_version=1,synthetic=True,kind='watermark',tenant_id=TENANTS[0],run_id=run_id,event_time_ms=window+94000)])
            due=min(deadline,time.monotonic()+60)
            while time.monotonic()<due:
                result=get('http://127.0.0.1:48000/runs/'+run_id)
                final=[r for r in result['snapshots'] if r['final'] and r['snapshot']['window_start_ms']==window and r['state']=='completed']
                if {r['tenant_id'] for r in final}==set(TENANTS): break
                time.sleep(1)
            else: raise TimeoutError('Window consumption')
            for row in final:
                expected,count=recount(events,row['tenant_id'])
                assert row['snapshot']['metrics']==expected and row['snapshot']['event_count']==count
                assert not row['invalidated'] and row['report']['external_model_calls']==0
            evidence['windows'].append({'window_start_ms':window,'scale':scale,'records':len(records),
                'event_package_sha256':sha256(json.dumps(records,sort_keys=True).encode()).hexdigest(),
                'final_snapshot_ids':[r['snapshot_id'] for r in final],'trace_ids':[r['report']['trace_id'] for r in final],'recount':'passed'})
            print(f'window {number+1}/{args.windows}: scale={scale}, records={len(records)}, exact recount passed',flush=True)
        evidence['operations']=get('http://127.0.0.1:48000/streaming-lab-api/operations')
        assert evidence['operations']['ledger']['pending']==evidence['operations']['ledger']['started']==evidence['operations']['ledger']['failed']==0
        evidence['results']=get('http://127.0.0.1:48000/runs/'+run_id)
        time.sleep(6)
        for trace_id in evidence['windows'][-1]['trace_ids']:
            trace=get('http://127.0.0.1:23200/api/traces/'+trace_id)
            (output/('trace-'+trace_id+'.json')).write_text(json.dumps(trace),encoding='utf-8')
        evidence['status']='passed'
    except Exception as exc:
        evidence.update(status='failed',error_type=type(exc).__name__,error=str(exc)[:200]);raise
    finally:
        evidence.update(finished_at=datetime.now(timezone.utc).isoformat(),published_records=sent,elapsed_seconds=time.monotonic()-began)
        atomic_json(output/'workload.json',evidence)
        # Remove only our exact ephemeral lock file, never a directory or data payload.
        if clock_path.read_text()==run_id: clock_path.unlink()
        print(str(output/'workload.json'),flush=True)


if __name__=='__main__': main()
