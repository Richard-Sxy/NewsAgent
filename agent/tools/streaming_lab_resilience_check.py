"""Restore-safe fault exercise of ONLY the isolated lab's Temporal, SQL and Flink."""
from datetime import datetime, timezone
import json
import time
from urllib.error import HTTPError, URLError

from streaming_lab_acceptance import ROOT, get, generate, recount, TENANTS, HTTP
from streaming_lab_ops import atomic_json, command, stop_with_savepoint, resume
from streaming_lab_workload import publish, allocate_clock, resources


def main():
    run_id='resilience-'+datetime.now().strftime('%Y%m%d-%H%M%S')
    directory=ROOT/'output/streaming-lab'/run_id;directory.mkdir(parents=True,exist_ok=False)
    record={'run_id':run_id,'started_at':datetime.now(timezone.utc).isoformat(),'checks':{}}
    stopped=set();job_stopped=False
    lock=ROOT/'var/streaming-lab/producer.lock'
    with lock.open('x') as out: out.write(run_id)
    def wait(label,probe,test,seconds=90):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                value=probe()
                if test(value): print(label+': passed',flush=True);return value
            except (HTTPError,URLError,ConnectionResetError,TimeoutError): pass
            time.sleep(1)
        raise TimeoutError(label)
    def stop(name):
        # Track intent first so finally restores even when the Docker response is lost.
        stopped.add(name);command(['docker','stop','--timeout','10',name],timeout=30)
    def start(name):
        command(['docker','start',name],timeout=30);stopped.discard(name)
    def api_ready(): return get('http://127.0.0.1:48000/ready')
    def agent_start(): return command(['docker','inspect','--format','{{.State.StartedAt}}','newsagent-streaming-lab-agent-1']).decode().strip()
    def readiness_code():
        try:
            with HTTP.open('http://127.0.0.1:48000/ready',timeout=8) as response:return response.status
        except HTTPError as exc: return exc.code
    try:
        sample=resources()
        if sample['windows_free_gib']<2 or sample['d_free_gib']<50: raise RuntimeError('Resource stop condition')
        record['resources']=sample
        api_ready();before_start=agent_start()
        stop('newsagent-streaming-lab-temporal-1')
        wait('Temporal outage reflected in readiness',readiness_code,lambda value:value==503)
        assert get('http://127.0.0.1:48000/live')['status']=='alive'
        window=allocate_clock(run_id,1);events=generate(run_id,window)
        publish(events)
        publish([dict(schema_version=1,synthetic=True,kind='watermark',tenant_id=TENANTS[0],run_id=run_id,event_time_ms=window+94000)])
        url='http://127.0.0.1:48000/runs/'+run_id
        pending=wait('Snapshots retained during Temporal outage',lambda:get(url),lambda d:len(d['snapshots'])==4)
        assert all(r['state']=='pending' for r in pending['snapshots'])
        start_attempts=command(['docker','exec','newsagent-streaming-lab-postgres-1','psql','-X','-U','stream_lab','-d','stream_lab','-At','-c',
            f"SELECT coalesce(sum(l.dispatch_attempts),0) FROM stream.consumption_ledger l JOIN stream.snapshots s USING(snapshot_id) WHERE s.run_id='{run_id}'"]).decode().strip()
        assert start_attempts=='0'
        start('newsagent-streaming-lab-temporal-1')
        wait('Temporal and dispatcher recovered',api_ready,lambda d:d['status']=='ok',120)
        completed=wait('Queued snapshots consumed',lambda:get(url),lambda d:len(d['snapshots'])==4 and all(r['state']=='completed' for r in d['snapshots']))
        for row in completed['snapshots']:
            expected,count=recount(events,row['tenant_id'])
            assert row['snapshot']['metrics']==expected and row['snapshot']['event_count']==count
        assert agent_start()==before_start
        record['checks']['temporal_outage_backlog_zero_spent_attempts_same_process_recovery']='passed'
        job_stopped=True;record['savepoint']=stop_with_savepoint()
        stop('newsagent-streaming-lab-postgres-1')
        wait('SQL outage reflected in readiness',readiness_code,lambda code:code==503)
        assert get('http://127.0.0.1:48000/live')['status']=='alive'
        start('newsagent-streaming-lab-postgres-1')
        wait('SQL and dispatcher recovered',api_ready,lambda d:d['status']=='ok',120)
        assert agent_start()==before_start
        assert get(url)['snapshots']==completed['snapshots']
        record['checks']['sql_outage_same_process_recovery_reports_unchanged']='passed'
        command(['docker','restart','--timeout','10','newsagent-streaming-lab-jobmanager-1'],timeout=30)
        wait('JobManager restarted',lambda:get('http://127.0.0.1:48081/jobs/overview'),lambda d:not d['jobs'],60)
        record['resume']=resume();job_stopped=False
        assert record['resume']['state']=='resumed'
        record['checks']['jobmanager_restart_savepoint_resume']='passed'
        # Confirm actual new input after state restore, rather than only a RUNNING label.
        after_window=allocate_clock(run_id+'-after',1)
        after_events=generate(run_id+'-after',after_window);publish(after_events)
        publish([dict(schema_version=1,synthetic=True,kind='watermark',tenant_id=TENANTS[0],run_id=run_id+'-after',event_time_ms=after_window+94000)])
        after=wait('Post-restore input consumed',lambda:get('http://127.0.0.1:48000/runs/'+run_id+'-after'),lambda d:len(d['snapshots'])==4 and all(r['state']=='completed' for r in d['snapshots']))
        for row in after['snapshots']:
            expected,count=recount(after_events,row['tenant_id'])
            assert row['snapshot']['metrics']==expected and row['snapshot']['event_count']==count
        record['checks']['post_restore_recount']='passed'
        record.update(status='passed',results=completed,post_restore_results=after,agent_started_at=before_start)
        time.sleep(6)
        for row in [r for r in after['snapshots'] if r['final']]:
            trace_id=row['report']['trace_id']
            (directory/('trace-'+trace_id+'.json')).write_text(json.dumps(get('http://127.0.0.1:23200/api/traces/'+trace_id)),encoding='utf-8')
    except Exception as exc:
        record.update(status='failed',error_type=type(exc).__name__,error=str(exc)[:300]);raise
    finally:
        restored={}
        for name in sorted(stopped):
            try: start(name);restored[name]='started'
            except Exception as exc: restored[name]=type(exc).__name__
        if job_stopped:
            try: restored['flink']=resume()
            except Exception as exc: restored['flink']=type(exc).__name__
        record.update(cleanup=restored,finished_at=datetime.now(timezone.utc).isoformat())
        atomic_json(directory/'resilience.json',record)
        if lock.read_text()==run_id: lock.unlink()
        print(str(directory/'resilience.json'),flush=True)


if __name__=='__main__': main()
