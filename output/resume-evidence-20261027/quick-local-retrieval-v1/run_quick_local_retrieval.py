"""Quick real-BGE local retrieval latency measurement; no enterprise claims."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,math,platform,sys,time
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'agent'))
from app.modules.retrieval_lab.engine import RetrievalLabEngine

out=Path(__file__).resolve().parent
if (out/'metrics.json').exists():
    raise SystemExit('Existing run preserved; choose a new directory')
package=ROOT/'var/news-events-annual-20261027/retrieval-lab-v1'
review=json.loads((ROOT/'output/news-events-annual-20261027/human-review-v1/human-review.export.json').read_text())
queries=review['runs']
engine=RetrievalLabEngine(package,ROOT/'var/knowledge-snapshots/rag-recall-demo/data/models/bge-large-zh-v1.5',device='cuda')
engine.encoder.log_path=out/'encoder.log'
samples=[]
try:
    build=engine.build()
    if engine.versions!=queries[0]['versions']:
        raise ValueError('Frozen version drift')
    for phase,repeats in [('warmup',1),('measurement',5)]:
        for repeat in range(repeats):
            for query in queries:
                started=time.perf_counter()
                try:
                    result=engine.search(query['query'],limit=10,category=query['category'])
                    actual=[n['news_id'] for n in result['candidates']]
                    expected=[n['news_id'] for n in query['candidates']]
                    row={'phase':phase,'repeat':repeat,'query_id':query['query_id'],
                        'elapsed_ms':(time.perf_counter()-started)*1000,
                        'status':'ok','top10_reproduced':actual==expected,
                        'trace':result['trace']}
                except Exception as exc:
                    row={'phase':phase,'repeat':repeat,'query_id':query['query_id'],
                         'elapsed_ms':(time.perf_counter()-started)*1000,
                         'status':'error','error_type':type(exc).__name__}
                samples.append(row)
                with (out/'samples.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        print(phase,'completed',flush=True)
finally:
    engine.close()

measured=[s for s in samples if s['phase']=='measurement']
success=[s for s in measured if s['status']=='ok']
values=sorted(s['elapsed_ms'] for s in success)
quantile=lambda q:values[max(0,math.ceil(q*len(values))-1)] if values else None
quality=json.loads((ROOT/'output/news-events-annual-20261027/human-review-v1/metrics.json').read_text())
report={
 'schema':'quick-local-real-bge-retrieval-v1','created_at':datetime.now(timezone.utc).isoformat(),
 'status':'completed' if len(success)==150 else 'completed_with_errors',
 'environment':'local_WSL_Ubuntu_20.04_RTX4070_numpy_exact',
 'enterprise_measurement':False,'independent_test':False,
 'python':platform.python_version(),'versions':engine.versions,
 'corpus_documents':545,'vector_chunks':2577,'query_count':30,
 'human_judgments':300,'warmup_requests':30,'measurement_requests':len(measured),
 'successful_requests':len(success),'failed_requests':len(measured)-len(success),
 'top10_reproduced_requests':sum(s.get('top10_reproduced',False) for s in success),
 'concurrency':1,'percentile_method':'nearest_rank_successful_requests_after_warmup',
 'latency_ms':{'p50':quantile(.5),'p95':quantile(.95),'p99':quantile(.99),'max':max(values) if values else None},
 'quality_existing_single_human_pool':quality['overall'],
 'scope':'Python engine.search including real query embedding IPC, exact cosine tier retrieval, news dedup, fusion and metadata; excluding initialization, HTTP, persistence, SQL lifecycle gates and production ANN.',
 'limitations':['30 development question queries, not independent related-news test.',
                'No paired RRF quality scores: 154 supplemental human labels pending.',
                'Request completion and frozen-output reproduction are not business-answer correctness.',
                'No LoRA training, report-writing quality experiment or human edit-time study performed.'],
 'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
(out/'metrics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:report[k] for k in ['status','successful_requests','top10_reproduced_requests','latency_ms']},ensure_ascii=False))
