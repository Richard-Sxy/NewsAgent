import pathlib,json,hashlib,sqlite3,collections,sys
import numpy as np
root=pathlib.Path('/home/shi/project/NewsAgent')
sys.path.insert(0,str(root/'agent'))
from app.modules.retrieval_lab.engine import chunk_news
p=root/'output/news-events-annual-20261027/comparison-v1'
comp=json.loads((p/'comparison.json').read_text())
config=comp['config']
for name,expected in config['source_code'].items():
 assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected,name
idx=root/'var/news-events-annual-20261027/retrieval-lab-v1/vector-index'
vectors=np.load(idx/'vectors.npy',allow_pickle=False)
news=[json.loads(s) for s in (idx.parent/'news.jsonl').read_text().splitlines()]
chunks=chunk_news(sorted(news,key=lambda n:n['news_id']))
extra5=0; common10=0
frozen=json.loads((root/'output/news-events-annual-20261027/human-review-v1/human-review.export.json').read_text())
known={(r['query_id'],n['news_id']) for r in frozen['runs'] for n in r['candidates']}
for q in comp['queries']:
 v=np.load(p/'query-vectors'/(q['query_id']+'.npy'),allow_pickle=False)
 raw=vectors@v
 allhits=[]
 rrf={}
 for tier in ['hot','warm','cold']:
  order=sorted((i for i,c in enumerate(chunks) if c['tier']==tier),key=lambda i:(-float(raw[i]),chunks[i]['chunk_id']))
  ids=[]
  for i in order:
   nid=chunks[i]['news_id']
   if nid in ids:continue
   ids.append(nid)
   rrf[nid]=config['weights'][tier]/(config['rrf_k']+len(ids))
   allhits.append((nid,chunks[i]['chunk_id'],float(raw[i])))
   if len(ids)>=20:break
 expectedrrf=sorted(rrf,key=lambda nid:(-rrf[nid],nid))[:10]
 expectedglobal=[n[0] for n in sorted(allhits,key=lambda n:(-n[2],n[0],n[1]))[:10]]
 assert expectedrrf==[n['news_id'] for n in q['predictions']['legacy_rrf']]
 assert expectedglobal==[n['news_id'] for n in q['predictions']['global_similarity']]
 extra5+=sum((q['query_id'],n['news_id']) not in known for n in q['predictions']['legacy_rrf'][:5])
 common10+=len(set(expectedrrf)&set(expectedglobal))
with sqlite3.connect('file:'+str(p/'supplemental.sqlite3')+'?mode=ro',uri=True) as db:
 assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
 print('supplement_counts',{t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['lab_runs','lab_run_candidates','lab_judgments']})
with sqlite3.connect('file:'+str(root/'var/news-events-annual-20261027/retrieval-lab-v1/judgments.sqlite3')+'?mode=ro',uri=True) as db:
 live=db.execute('SELECT run_id,news_id,label,reviewer,notes,revision,updated_at FROM lab_judgments ORDER BY run_id,news_id').fetchall()
with sqlite3.connect('file:'+str(root/'output/news-events-annual-20261027/human-review-v1/judgments.snapshot.sqlite3')+'?mode=ro',uri=True) as db:
 assert live==db.execute('SELECT run_id,news_id,label,reviewer,notes,revision,updated_at FROM lab_judgments ORDER BY run_id,news_id').fetchall()
print(json.dumps({'independent_vector_replay_queries':len(comp['queries']),'source_code_hashes':'matched','new_unjudged_in_rrf_top5':extra5,'top10_overlap_pairs':common10,'original_300_labels':'unchanged'},ensure_ascii=False))
