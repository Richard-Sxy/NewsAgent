"""Exercise an acknowledgement gap and immutable snapshots in ONLY the new lab."""
import argparse
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.error import URLError
from urllib.request import build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[2]
HTTP=build_opener(ProxyHandler({}))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('run_id')
    args=parser.parse_args()
    if not re.fullmatch('[a-z0-9-]{1,50}',args.run_id):raise ValueError('Invalid run ID')
    directory=ROOT/'output'/'streaming-lab'/args.run_id
    evidence={'started_at':datetime.now(timezone.utc).isoformat(),'commands':[]}

    def read():
        with HTTP.open('http://127.0.0.1:48000/runs/'+args.run_id,timeout=4) as r:return json.load(r)

    def cmd(name,args,expect_failure=False):
        p=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',timeout=30)
        evidence['commands'].append({'name':name,'command':args,'exit_code':p.returncode,'stdout':p.stdout[-1200:],'stderr':p.stderr[-1200:]})
        if bool(p.returncode)!=expect_failure:raise RuntimeError(name+' unexpected result')
        return p

    def sql(name,text,expect_failure=False):
        return cmd(name,['docker','exec','newsagent-streaming-lab-postgres-1','psql','-X','-U','stream_lab','-d','stream_lab','-v','ON_ERROR_STOP=1','-c',text],expect_failure)

    stopped=False
    try:
        before=read()
        assert all(r['state']=='completed' for r in before['snapshots'])
        target=next(r for r in before['snapshots'] if r['final'])
        sid=target['snapshot_id'];assert re.fullmatch('[0-9a-f]{64}',sid)
        digest=lambda r:sha256(json.dumps(r,sort_keys=True).encode()).hexdigest()
        previous_hash=digest(target['report'])
        refused=sql('snapshot_update_rejected',f"BEGIN; UPDATE stream.snapshots SET payload_text=payload_text WHERE snapshot_id='{sid}'; ROLLBACK;",True)
        assert 'Published snapshots are immutable' in refused.stderr
        evidence['immutable_snapshot']='passed'
        cmd('stop_only_lab_agent',['docker','stop','--timeout','15','newsagent-streaming-lab-agent-1']);stopped=True
        sql('simulate_missing_dispatch_ack',f"UPDATE stream.consumption_ledger SET state='pending',dispatch_attempts=0 WHERE snapshot_id='{sid}';")
        cmd('start_same_lab_agent',['docker','start','newsagent-streaming-lab-agent-1']);stopped=False
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            try:
                after=read();row=next(r for r in after['snapshots'] if r['snapshot_id']==sid)
                if row['state']=='completed':break
            except (URLError,TimeoutError,ConnectionResetError):pass
            time.sleep(1)
        else:raise TimeoutError('Ledger reconciliation did not finish')
        assert len(after['snapshots'])==len(before['snapshots'])
        assert digest(row['report'])==previous_hash
        assert row['workflow_id']==target['workflow_id']
        evidence.update(status='passed',ledger_replay='passed',same_report_and_trace='passed',snapshot_id=sid,workflow_id=row['workflow_id'],report_sha256=previous_hash,snapshot_count=len(after['snapshots']))
    except Exception as exc:
        evidence.update(status='failed',error_type=type(exc).__name__)
        raise
    finally:
        if stopped:subprocess.run(['docker','start','newsagent-streaming-lab-agent-1'],timeout=30,check=False)
        evidence['finished_at']=datetime.now(timezone.utc).isoformat()
        (directory/'recovery.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in evidence.items() if k!='commands'},ensure_ascii=False))


if __name__=='__main__':main()
