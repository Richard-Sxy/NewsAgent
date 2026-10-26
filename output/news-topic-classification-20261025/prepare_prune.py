"""Prepare exact retention/deletion candidates without changing any source or DB."""
import hashlib
import json
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
ALLOW = {'tech', 'ai', 'finance', 'auto', 'international', 'military', 'sports', 'games'}
NAMES = {'tech':'科技', 'ai':'AI', 'finance':'财经', 'stocks':'股票', 'auto':'汽车',
         'digital':'数码', 'international':'国际', 'military':'军事', 'sports':'体育', 'games':'游戏'}
STOCKS = set('股票 股价 股市 A股 港股 美股 沪指 深成指 创业板 科创板 涨停 跌停 涨幅 跌幅 股东 股份 股息 市盈率 市值 中证 恒生 标普 纳指 道指 沪深 新三板 融券 融资融券'.split())
DIGITAL = set('iPhone iPad MacBook 苹果手表 华为手机 小米手机 荣耀手机 OPPO vivo 三星手机 一加 红米 Redmi 骁龙 天玑 手机 折叠屏 智能手表 平板电脑 笔记本电脑 显卡 主板 内存 硬盘 鸿蒙 HarmonyOS iOS Android 安卓 数码 相机 徕卡 光学变焦 扫地机 智能家居'.split())


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf8')


def classify_action(row):
    if row['primary_category'] in ALLOW:
        return 'keep_target'
    if row['primary_category']=='unclassified':
        return 'hold_unclassified'
    if 'close_topic_scores' in row['review_flags']:
        return 'hold_close_scores'
    if any(k in ALLOW for k in row['secondary_candidates']):
        return 'hold_target_secondary'
    return 'remove_other'


def target_category(row):
    category=row['primary_category']
    terms=set(row.get('evidence',{}).get(category,{}).get('title_terms',[]))
    # Split only already-retained parent topics using title evidence.
    # Broad ETF or chip mentions alone do not force stocks/digital.
    if category=='finance' and terms & STOCKS:
        return 'stocks'
    if category=='tech' and terms & DIGITAL:
        return 'digital'
    return category


def main():
    directory=BASE/'prune-plan'
    directory.mkdir(exist_ok=False)
    source=BASE/'results-v2/classification.jsonl'
    rows=[json.loads(line) for line in source.open(encoding='utf8')]
    plan=[]
    for row in rows:
        action=classify_action(row)
        category=target_category(row) if action=='keep_target' else None
        plan.append({'news_id':row['news_id'],'record_sha256':row['record_sha256'],
                     'display_title':row['display_title'],'content_state':row['content_state'],
                     'original_primary':row['primary_category'],'original_primary_name':row['primary_name'],
                     'target_category':category,'target_category_name':NAMES.get(category),
                     'action':action,'review_flags':row['review_flags'],
                     'secondary_candidates':row['secondary_candidates']})
    for name,predicate in [('decisions',lambda r:True),
                           ('conservative-remove',lambda r:r['action']=='remove_other'),
                           ('strict-remove',lambda r:r['action']!='keep_target'),
                           ('hold-for-review',lambda r:r['action'].startswith('hold_'))]:
        with (directory/(name+'.jsonl')).open('x',encoding='utf8') as output:
            for row in plan:
                if predicate(row):
                    output.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n')
    counts=Counter(r['action'] for r in plan)
    summary={'status':'prepared_not_applied','source_classification_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
             'tenant_id':'public-news-local','source_release_id':'news-0c74530f2864db20a73d7a96',
             'requested_categories':list(NAMES.values()),'total':len(plan),'actions':dict(counts),
             'target_categories':dict(Counter(r['target_category_name'] for r in plan if r['action']=='keep_target')),
             'conservative':{'keep':len(plan)-counts['remove_other'],'remove':counts['remove_other']},
             'strict':{'keep':counts['keep_target'],'remove':len(plan)-counts['keep_target']},
             'source_content_counts':dict(Counter(r['content_state'] for r in plan)),
             'limitations':['Rule-inferred labels are not gold.','Stocks/digital are title-evidence subdivisions of finance/tech.',
                            'No news, source file, database or index has been deleted by this script.']}
    write_json(directory/'summary.json',summary)
    write_json(directory/'manifest.json',{'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}})
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
