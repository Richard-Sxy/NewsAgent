"""Bounded local operations. No .env, global changes, deletion, or live-DB overwrite."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.request import ProxyHandler, build_opener

ROOT=Path(__file__).resolve().parents[2]
PG='newsagent-streaming-lab-postgres-1'
JM='newsagent-streaming-lab-jobmanager-1'
HTTP=build_opener(ProxyHandler({}))
JAR=ROOT/'var/streaming-lab/build/streaming-job.jar'


def get(path):
    with HTTP.open('http://127.0.0.1:48081'+path,timeout=5) as response: return json.load(response)


def command(args,*,input=None,timeout=60):
    result=subprocess.run(args,input=input,capture_output=True,timeout=timeout)
    if result.returncode: raise RuntimeError('Isolated operation failed: '+args[0]+' '+args[1])
    return result.stdout


def sql(database,source):
    return command(['docker','exec','-i',PG,'psql','-X','-U','stream_lab','-d',database,'-At','-v','ON_ERROR_STOP=1'],input=source.encode()).decode().strip()


def atomic_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('x',encoding='utf-8') as out: json.dump(data,out,ensure_ascii=False,indent=2)
    temporary.replace(path)


def stop_with_savepoint():
    jobs=[j for j in get('/jobs/overview')['jobs'] if j['state']=='RUNNING']
    if len(jobs)!=1: raise RuntimeError('Expected exactly one running lab Flink job')
    job_id=jobs[0]['jid']
    if not re.fullmatch('[0-9a-f]{32}',job_id): raise RuntimeError('Invalid job ID')
    # Durable intent before the side effect; one call only. Never resubmit on timeout.
    directory=ROOT/'var/streaming-lab/recovery'
    intent=directory/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-intent.json')
    manifest={'job_id':job_id,'jar_sha256':sha256(JAR.read_bytes()).hexdigest(),
              'created_at':datetime.now(timezone.utc).isoformat(),'state':'stop_requested'}
    atomic_json(intent,manifest)
    output=command(['docker','exec',JM,'/opt/flink/bin/flink','stop','--savepointPath','file:///state/savepoints',job_id],timeout=60).decode()
    match=re.search(r'(file:/+state/savepoints/savepoint-[a-zA-Z0-9_-]+)',output)
    if not match: raise RuntimeError('Savepoint location not returned; inspect existing job before retrying')
    manifest.update(savepoint=match[1],state='saved')
    atomic_json(directory/'latest.json',manifest)
    return manifest


def resume():
    active=[j for j in get('/jobs/overview')['jobs'] if j['state'] not in ('FINISHED','FAILED','CANCELED','SUSPENDED')]
    if active: return {'state':'already_active','jobs':[j['jid'] for j in active]}
    manifest=json.loads((ROOT/'var/streaming-lab/recovery/latest.json').read_text(encoding='utf-8'))
    if manifest['jar_sha256']!=sha256(JAR.read_bytes()).hexdigest(): raise ValueError('JAR changed; state upgrade requires explicit review')
    savepoint=manifest['savepoint']
    if not re.fullmatch(r'file:/+state/savepoints/savepoint-[a-zA-Z0-9_-]+',savepoint): raise ValueError('Savepoint outside lab state directory')
    output=command(['docker','exec',JM,'/opt/flink/bin/flink','run','-d','--fromSavepoint',savepoint,'-c','lab.StreamingJob','/lab/streaming-job.jar']).decode()
    match=re.search(r'JobID\s+([0-9a-f]{32})',output)
    if not match: raise RuntimeError('Job ID unavailable; inspect jobs before any retry')
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        state=get('/jobs/'+match[1])['state']
        if state=='RUNNING': return {'state':'resumed','job_id':match[1],'savepoint':savepoint}
        if state in ('FAILED','CANCELED'): raise RuntimeError('Restored job failed')
        time.sleep(1)
    raise TimeoutError('Resume did not become running')


def digest_database(database):
    # Excludes volatile lease/heartbeat times; includes immutable snapshots, reports and lineage.
    return json.loads(sql(database,"""BEGIN READ ONLY;
      SELECT json_build_object(
        'snapshots',(SELECT count(*) FROM stream.snapshots),
        'snapshot_hash',(SELECT md5(coalesce(string_agg(snapshot_id||payload_text,'' ORDER BY snapshot_id),'')) FROM stream.snapshots),
        'reports',(SELECT count(*) FROM stream.reports),
        'report_hash',(SELECT md5(coalesce(string_agg(snapshot_id||report::text,'' ORDER BY snapshot_id),'')) FROM stream.reports),
        'backfills',(SELECT count(*) FROM stream.backfills),
        'lineage_hash',(SELECT md5(coalesce(string_agg(request_id||snapshot_id||base_snapshot_id,'' ORDER BY request_id),'')) FROM stream.backfills)
      ); COMMIT;""").splitlines()[1])


def backup_restore_check():
    if shutil.disk_usage('D:\\').free<50*1024**3: raise RuntimeError('D drive free space stop condition')
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    directory=ROOT/'output/streaming-lab'/('backup-'+stamp)
    directory.mkdir(parents=True,exist_ok=False)
    # Operator must choose a quiet moment; do not assert consistency across concurrent publications.
    if int(sql('stream_lab',"SELECT count(*) FROM stream.consumption_ledger WHERE state IN ('pending','started')")):
        raise RuntimeError('Wait for consumption to drain before a verified backup')
    before=digest_database('stream_lab')
    dump=directory/'stream.dump'
    with dump.open('xb') as out:
        result=subprocess.run(['docker','exec',PG,'pg_dump','-U','stream_lab','-d','stream_lab','-Fc','--no-owner','--no-acl'],stdout=out,stderr=subprocess.PIPE,timeout=60)
    if result.returncode: raise RuntimeError('Backup failed; partial dump retained for diagnosis')
    after=digest_database('stream_lab')
    if before!=after: raise RuntimeError('Source changed during backup; do not label this dump verified')
    target='stream_restore_'+datetime.now().strftime('%Y%m%d_%H%M%S')
    command(['docker','exec',PG,'createdb','-U','stream_lab','-T','template0',target])
    with dump.open('rb') as source:
        result=subprocess.run(['docker','exec','-i',PG,'pg_restore','-U','stream_lab','-d',target,'--no-owner','--no-acl','--exit-on-error','--single-transaction'],stdin=source,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
    if result.returncode: raise RuntimeError('Restore failed; isolated database retained for diagnosis')
    restored=digest_database(target)
    if restored!=before: raise RuntimeError('Restored snapshot/report/lineage differs')
    report=dict(status='passed',source_database='stream_lab',restore_database=target,
                dump_sha256=sha256(dump.read_bytes()).hexdigest(),dump_bytes=dump.stat().st_size,
                immutable_data=restored,scope='Isolated stream database only; not Temporal or Kafka disaster recovery')
    atomic_json(directory/'verification.json',report)
    return {**report,'report':str(directory/'verification.json')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['status','savepoint-stop','resume','backup-restore-check'])
    args=parser.parse_args()
    result={'status':lambda:get('/jobs/overview'),'savepoint-stop':stop_with_savepoint,
            'resume':resume,'backup-restore-check':backup_restore_check}[args.action]()
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__': main()
