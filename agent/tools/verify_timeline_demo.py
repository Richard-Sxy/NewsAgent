"""Verify the isolated local frontend proxy, export dataset identity and three completed runs."""
import json,time,urllib.request
from pathlib import Path
base='http://127.0.0.1:5175'
headers={'Content-Type':'application/json'}
def request(path,body=None,timeout=140):
    req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
    with urllib.request.urlopen(req,timeout=timeout) as response:return json.load(response)
for attempt in range(90):
    try:
        config=request('/api/v1/local-simulation/hot-news/sql-config',timeout=5)
        break
    except Exception:
        time.sleep(5)
else:raise RuntimeError('new dataset API did not become ready')
dataset=config['dataset']
assert dataset['dataset_profile']=='timeline-v4'
assert dataset['total_metric_row_count']==1728000
assert dataset['hours_per_news']==720
print(json.dumps({key:dataset[key] for key in ['dataset_profile','news_count','total_metric_row_count','window_start','window_end']},ensure_ascii=False),flush=True)
output=Path(__file__).resolve().parents[1] / 'data' / 'timeline-v4'
output.mkdir(exist_ok=True)
(output/'manifest.json').write_text(json.dumps(dataset,ensure_ascii=False,indent=2)+'\n')
runs=[]
for date in ['2026-10-09','2026-10-02','2026-09-10']:
    body=dict(question='查询点击量最高的前5条新闻',scenario_id='news-ranking',
              window_start=date+'T19:00:00+08:00',window_end=date+'T20:00:00+08:00')
    result=request('/api/v1/local-simulation/hot-news/run',body)
    print(json.dumps({'date':date,**result},ensure_ascii=False),flush=True)
    assert result['status']=='completed'
    detail=request('/api/v1/hot-news/runs/'+result['run_id'])
    assert len(detail['ranked_news'])==5
    assert all(item['title'].startswith('[合成热点样本]') for item in detail['ranked_news'])
    runs.append({'date':date,'run_id':result['run_id'],'status':result['status'],'ranked_news_count':len(detail['ranked_news'])})
(output/'verification.json').write_text(json.dumps({'dataset_sha256':dataset['dataset_sha256'],'runs':runs},ensure_ascii=False,indent=2)+'\n')
