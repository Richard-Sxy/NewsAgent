from pathlib import Path
import tempfile,shutil,sqlite3,subprocess,json,sys
root=Path(__file__).resolve().parents[2]
python=Path(sys.executable)
with tempfile.TemporaryDirectory(prefix='newsagent-transfer-') as temp:
 target=Path(temp)/'different-checkout'
 target.mkdir()
 shutil.copytree(root/'agent/app',target/'agent/app',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
 (target/'agent/tools').mkdir()
 for name in ['compare_annual_retrieval.py','verify_annual_retrieval_transfer.py']:
  shutil.copy2(root/'agent/tools'/name,target/'agent/tools'/name)
 rel=Path('output/news-events-annual-20261027')
 shutil.copytree(root/rel/'human-review-v1',target/rel/'human-review-v1')
 compare=target/rel/'comparison-v1'
 compare.mkdir()
 for name in ['comparison.json','config.json','evidence-manifest.json']:
  shutil.copy2(root/rel/'comparison-v1'/name,compare/name)
 shutil.copytree(root/rel/'comparison-v1/query-vectors',compare/'query-vectors')
 with sqlite3.connect('file:'+str(root/rel/'comparison-v1/supplemental.sqlite3')+'?mode=ro',uri=True) as source:
  with sqlite3.connect(compare/'supplemental.sqlite3') as dest:source.backup(dest)
 data=Path('var/news-events-annual-20261027/retrieval-lab-v1')
 (target/data/'vector-index').mkdir(parents=True)
 for name in ['manifest.json','news.jsonl','queries.jsonl','relations.jsonl','event-chains.jsonl','vector-index/manifest.json','vector-index/vectors.npy']:
  shutil.copy2(root/data/name,target/data/name)
 assert not (target/'var/knowledge-snapshots').exists()
 result=subprocess.run([str(python),'-B',str(target/'agent/tools/compare_annual_retrieval.py'),'score'],
   cwd=target,capture_output=True,text=True,check=True)
 scored=json.loads((compare/'results.json').read_text())
 assert scored['input_review']['directory']==str(target/rel/'human-review-v1')
 assert scored['queries']==30 and scored['required_pool_pairs']==454
 assert (scored['metrics'] is None)==bool(scored['missing_pairs'])
 verify=subprocess.run([str(python),'-B',str(target/'agent/tools/verify_annual_retrieval_transfer.py')],
   cwd=target,capture_output=True,text=True,check=True)
 verified=json.loads(verify.stdout)
 assert verified['queries_replayed']==30
 testcode="""import sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'agent'))
from fastapi.testclient import TestClient
from app.modules.retrieval_lab.api import create_app
from app.modules.retrieval_lab.comparison import ComparisonReviewService,SupplementalStore
directory=Path('output/news-events-annual-20261027/comparison-v1')
client=TestClient(create_app(ComparisonReviewService(directory)),base_url='http://127.0.0.1:8879')
assert client.get('/api/status').status_code==200
run=client.get('/api/queries').json()['items'][0]
item=client.get('/api/runs/'+run['run_id']).json()['candidates'][0]
response=client.put('/api/runs/'+run['run_id']+'/judgments/'+item['news_id'],
 json={'label':'irrelevant','reviewer':'TRANSFER-TEST-ONLY','notes':'isolated temporary copy','expected_revision':item['judgment']['revision']},
 headers={'X-Lab-Request':'1'})
assert response.status_code==200,response.text
reloaded=SupplementalStore(directory/'supplemental.sqlite3').get_run(run['run_id'])
assert reloaded['candidates'][0]['judgment']['reviewer']=='TRANSFER-TEST-ONLY'
print('isolated_http_save_reopen_passed')
"""
 ui=subprocess.run([str(python),'-B','-c',testcode],cwd=target,capture_output=True,text=True,check=True)
 print(json.dumps({'relocated_checkout_scoring':'passed','local_review_path_selected':True,
   'model_weights_present':False,'real_saved_vector_replay_queries':verified['queries_replayed'],
   'isolated_http_save_reopen':ui.stdout.strip(),'real_annotation_database_modified':False},ensure_ascii=False))
