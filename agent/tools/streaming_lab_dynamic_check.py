"""Bounded live acceptance: pause, worker restart, resume, changing traffic and trace evidence."""
from datetime import datetime, timezone
import json
import sys
import time

from streaming_lab_acceptance import ROOT, get, recount, TENANTS
from streaming_lab_ops import command, atomic_json, sql
from streaming_lab_workload import resources, enforce_budget

sys.path.insert(0,str(ROOT/'agent'))
from simulation.streaming_lab.dynamic_data import packet, digest

BASE='http://127.0.0.1:48000'
WORKER='newsagent-streaming-lab-dynamic-feed-1'


def control(action):
    return json.loads(command(['docker','exec',WORKER,'python','-m','simulation.streaming_lab.dynamic_feed',action],timeout=25))


def main():
    output=ROOT/'output/streaming-lab'/('dynamic-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True,exist_ok=False)
    evidence=dict(started_at=datetime.now(timezone.utc).isoformat(),status='running',checks={},batches=[])
    deadline=time.monotonic()+420
    try:
        control('pause')
        baseline=get(BASE+'/streaming-lab-api/operations')['dynamic_feed']
        sequence=baseline['sequence']
        # Pause is a drain, not an interruption of an already submitted batch.
        due=time.monotonic()+baseline['interval_seconds']+6
        while time.monotonic()<due:
            if time.monotonic()>deadline: raise TimeoutError('Acceptance deadline')
            time.sleep(3)
        paused=get(BASE+'/streaming-lab-api/operations')['dynamic_feed']
        assert paused['sequence']==sequence and not paused['enabled']
        assert not (ROOT/'var/streaming-lab/producer.lock').exists()
        evidence['checks']['pause_no_new_windows_and_release_clock']='passed'
        print('pause and stable clock: passed',flush=True)
        before=command(['docker','inspect','--format','{{.State.StartedAt}}',WORKER]).decode().strip()
        command(['docker','restart','--timeout','5',WORKER],timeout=20)
        control('resume')
        after=command(['docker','inspect','--format','{{.State.StartedAt}}',WORKER]).decode().strip()
        assert before!=after
        evidence['worker_restart']={'before':before,'after':after}
        seen=set()
        while len(seen)<4:
            if time.monotonic()>deadline: raise TimeoutError('Dynamic cycles deadline')
            ops=get(BASE+'/streaming-lab-api/operations')
            enforce_budget(resources(),ops)
            if ops['dynamic_feed']['last_error']: raise RuntimeError(ops['dynamic_feed']['last_error'])
            for batch in sorted(ops['dynamic_feed']['recent_batches'],key=lambda b:b['sequence']):
                if batch['sequence']<=sequence or batch['sequence'] in seen or batch['state']!='completed': continue
                value=packet(batch['sequence'],batch['window_start_ms'],batch['generator_version'])
                result=get(BASE+'/runs/'+batch['run_id'])
                final=[r for r in result['snapshots'] if r['final'] and r['is_latest']]
                assert len(final)==2 and not result['rejections']
                for row in final:
                    expected,count=recount(value['events'],row['tenant_id'])
                    assert row['snapshot']['metrics']==expected and row['snapshot']['event_count']==count
                    assert row['state']=='completed' and not row['invalidated']
                    assert row['report']['external_model_calls']==0
                evidence['batches'].append(dict(**batch,packet_sha256=digest(value),results=result))
                seen.add(batch['sequence'])
                print(batch['run_id']+': exact recount and analysis passed',flush=True)
                atomic_json(output/'acceptance.json',evidence)
            time.sleep(3)
        evidence['checks']['worker_restart_same_workflow_continues']='passed'
        evidence['checks']['four_changing_windows_exact_recount']='passed'
        evidence['sql_projection']=json.loads(sql('stream_lab',"SELECT json_agg(x) FROM (SELECT run_id,tenant_id,sum(clicks) AS clicks,count(*) AS news_count FROM stream.news_metric_current WHERE run_id LIKE 'live-%' GROUP BY run_id,tenant_id ORDER BY run_id,tenant_id) x;"))
        for batch in evidence['batches']:
            for tenant in TENANTS:
                expected,_=recount(packet(batch['sequence'],batch['window_start_ms'],batch['generator_version'])['events'],tenant)
                projected=next(r for r in evidence['sql_projection'] if r['run_id']==batch['run_id'] and r['tenant_id']==tenant)
                assert projected['clicks']==sum(r['clicks'] for r in expected) and projected['news_count']==3
        evidence['checks']['dynamic_sql_projection']='passed'
        leaders={r['report']['ranking'][0]['news_id'] for b in evidence['batches'] for r in b['results']['snapshots'] if r['final'] and r['tenant_id']==TENANTS[0]}
        assert len(leaders)>=2, 'Expected changing Python hot-ranking leaders'
        evidence['checks']['python_rank_leader_changes']='passed'
        time.sleep(6)
        last=evidence['batches'][-1]
        trace_ids=[last['source_trace_id']]+[r['report']['trace_id'] for r in last['results']['snapshots'] if r['final']]
        for trace_id in trace_ids:
            atomic_json(output/('trace-'+trace_id+'.json'),get('http://127.0.0.1:23200/api/traces/'+trace_id))
        evidence['checks']['source_and_two_report_traces_retrieved']='passed'
        evidence['operations']=get(BASE+'/streaming-lab-api/operations')
        evidence['legacy_v8_ready']=get('http://127.0.0.1:38000/ready')
        evidence['resources']=resources()
        evidence['status']='passed'
    except Exception as exc:
        evidence.update(status='failed',error_type=type(exc).__name__,error=str(exc)[:200])
        control('pause')
        raise
    finally:
        evidence['finished_at']=datetime.now(timezone.utc).isoformat()
        atomic_json(output/'acceptance.json',evidence)
        print(str(output/'acceptance.json'),flush=True)


if __name__=='__main__': main()
