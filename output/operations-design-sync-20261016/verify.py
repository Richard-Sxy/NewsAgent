"""Read-only checks for the operations design update; never loads dotenv."""
from pathlib import Path
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
AGENT = ROOT / 'agent'
NODE = '/Users/shi/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'

def digest(path):
    return sha256(path.read_bytes()).hexdigest()

def save(name, data):
    (OUT/name).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def utc():
    return datetime.now(timezone.utc).isoformat()

paths = set()
for folder in ['app/audience', 'evaluation', 'tests']:
    base = AGENT/folder
    if folder == 'evaluation':
        paths.update(base.glob('*.py'))
    elif folder == 'tests':
        paths.update(base.glob('test_audience*.py'))
        paths.update(base.glob('test_data_writing*.py'))
    else:
        paths.update(base.rglob('*.py'))
for rel in ['app/api/audience.py', 'app/config.py', 'app/main.py',
            'app/observability/telemetry.py', 'app/schemas/hot_news.py',
            'frontend/tests/audience-preview.test.mjs',
            'frontend/src/api/audience.ts',
            'frontend/src/components/hotnews/AudiencePreviewPanel.vue',
            'deploy/audience-topics.local.yml', 'frontend/package-lock.json']:
    p = AGENT/rel
    if p.exists():
        paths.add(p)
for folder in ['evaluation/datasets/data_writing_august_2026_v1',
               'evaluation/datasets/operations_evolution_v2']:
    paths.update(p for p in (AGENT/folder).iterdir() if p.suffix in {'.json','.jsonl'})
frozen = {str(p.relative_to(ROOT)): digest(p) for p in sorted(paths)}
tracked = subprocess.run(['git','ls-files','--',*frozen], cwd=ROOT, capture_output=True, check=True).stdout.decode().splitlines()
diff = subprocess.run(['git','diff','--binary','HEAD','--',*tracked],cwd=ROOT,capture_output=True,check=True).stdout
version = {}
for name in ['pydantic','PyYAML','pytest','httpx','fastapi','reportlab']:
    try:
        version[name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        version[name] = None

commands = [
    ('writing_unit', [sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_data_writing*.py'], AGENT),
    ('writing_artifacts', [sys.executable,'-B','-m','evaluation.data_writing_dataset','--seed','evaluation/datasets/data_writing_august_2026_v1/seed.json','--output-dir','evaluation/datasets/data_writing_august_2026_v1','--check'], AGENT),
    ('evolution_artifacts', [sys.executable,'-B','-m','evaluation.run_operations_evolution','validate','--verify-artifacts'], AGENT),
    ('audience_frontend', [NODE,'--test','tests/audience-preview.test.mjs'], AGENT/'frontend'),
]
spec = {
    'schema_version':1, 'status':'frozen_for_offline_checks', 'frozen_at':utc(),
    'question':'新版主题受众业务定位与现有离线资产、组件契约是否一致',
    'targets':['deterministic_regression','artifact_integrity','frontend_component_contract'],
    'scope':{'entry':'existing read-only dataset CLIs, unittest and node:test',
             'exit':'native test summaries and artifact-hash reports',
             'exclude':['enterprise HTTP','live database','Temporal execution','LLM','deployment','approval','dispatch'],
             'allowed_claims':['only the executed offline assertions and package integrity']},
    'authorization':{'basis':'用户要求同步最新运营建议相关内容；离线可逆核对',
                     'production_mutation':False,'allowed_side_effects':['new audit files in this directory']},
    'code':{'git_head':subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip(),
            'relevant_tracked_diff_sha256':sha256(diff).hexdigest(),
            'files_byte_sha256':frozen,'dependency_versions':version,
            'python':sys.executable,'python_version':sys.version,
            'fingerprint_scope':'selected directly relevant source/data files; not a production image identity'},
    'data':{'writing_cases':27,'writing_event_groups':9,'evolution_judgments':432,
            'review_status':'draft/human labels pending; no model scoring',
            'as_of':'each original case timestamp retained; not replaced by execution date',
            'timezone':'Asia/Shanghai'},
    'runtime':{'inference':'not_used','embedding':'not_used','warehouse':'not_used',
               'workflow':'not_used','audience':'front-end fixture only; no enterprise service',
               'dotenv':'not loaded','credential_values_included':False},
    'scoring':{'oracle':'existing unittest/node assertions and byte-for-byte artifact checks',
               'acceptance':'all selected checks exit 0 and no frozen-file drift',
               'quality_thresholds':'not applicable; no model or business quality score',
               'repetitions':1,'failure_denominator_rule':'all planned checks retained; missing dependency is blocked'},
    'budget':{'max_model_calls':0,'max_seconds_per_command':120,'max_concurrency':1,
              'stop_conditions':['source/data drift blocks overall acceptance','timeout retained as execution failure']},
    'execution':{'commands':[{'name':n,'argv':a,'cwd':str(w)} for n,a,w in commands]},
    'not_run':{'audience_backend_contract':'current selected interpreters lack pytest/httpx/fastapi; no dependency installation',
               'model_quality':'no approved labels/model run selected',
               'enterprise_contract':'no real endpoint selected',
               'business_effect':'no live operations trial selected'},
}
if (OUT/'run-spec.json').exists():
    raise SystemExit('Run already exists; preserve it and choose a new directory.')
save('run-spec.json',spec)
results=[]
for name, argv, cwd in commands:
    started=utc()
    t=time.monotonic()
    try:
        p=subprocess.run(argv,cwd=cwd,capture_output=True,text=True,timeout=120,
                         env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        output=p.stdout+p.stderr
        code=p.returncode
        state='passed' if code==0 else ('blocked' if 'ModuleNotFoundError' in output else 'failed')
    except subprocess.TimeoutExpired as exc:
        output=str(exc)
        code=None
        state='failed'
    (OUT/(name+'.txt')).write_text(output,encoding='utf-8')
    item={'name':name,'status':state,'exit_code':code,'started_at':started,
          'finished_at':utc(),'elapsed_seconds':round(time.monotonic()-t,3),
          'log':name+'.txt','argv':argv,'cwd':str(cwd)}
    if name=='writing_unit' and code==0:
        m=re.search(r'Ran (\d+) tests',output)
        item['test_count']=int(m.group(1)) if m else None
    if name=='audience_frontend':
        for key in ['tests','pass','fail','skipped','cancelled','todo']:
            m=re.search(r'^# '+key+r' (\d+)\s*$',output,re.M)
            item[key]=int(m.group(1)) if m else None
    results.append(item)
    print(name,state,flush=True)
after={str(p.relative_to(ROOT)):digest(p) for p in sorted(paths)}
drift=[name for name in frozen if frozen[name]!=after[name]]
overall='passed' if not drift and all(x['status']=='passed' for x in results) else 'partial'
save('results.json',{'schema_version':1,'finished_at':utc(),'status':overall,
                     'checks':results,'frozen_file_drift':drift,'not_run':spec['not_run'],
                     'quality_acceptance':'blocked_pending_human_labels_and_real_contract',
                     'dispatch_executed':False,'model_calls':0})
print('overall',overall,flush=True)
