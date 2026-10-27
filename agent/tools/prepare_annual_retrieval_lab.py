"""Freeze verified annual public news into an isolated content DB and lab corpus."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent'))
sys.path.insert(0, str(ROOT / 'agent' / 'tools'))
from prepare_annual_news_review import verify_records, content_candidates, load_discovery, CATEGORIES, exact_aware_time
from app.modules.news_storage.model import build_package, canonical_json
from app.modules.news_storage.package_io import save_package
from app.modules.news_storage.store import FrozenNewsStore
from sqlalchemy import create_engine

POLICY = 'annual-date-tiers-exact-body-v1'


def digest(value):
    return sha256(canonical_json(value).encode('utf-8')).hexdigest()


def tier_for(published, as_of):
    age = (as_of - date.fromisoformat(published)).days
    if age < 0:
        raise ValueError('future publication is outside the frozen snapshot')
    return 'hot' if age <= 7 else 'warm' if age <= 90 else 'cold'


def prepare_records(records, groups, *, as_of):
    by_body = defaultdict(list)
    for r in records:
        tier_for(r['publication_date'], as_of)
        by_body[r['source_evidence']['body_sha256']].append(r)
    articles, aliases = [], {}
    for body_hash, members in sorted(by_body.items()):
        primary = min(members, key=lambda r: (r['publication_date'], r['url']))
        nid = 'pub-' + primary['article_id']
        for r in members:
            aliases['pub-' + r['article_id']] = nid
        article = {
            'news_id': nid, 'title': primary['title'], 'body': primary['body'],
            'url': primary['url'], 'publication_date': primary['publication_date'],
            'publish_time': exact_aware_time(primary), 'date_precision': primary['date_precision'],
            'published_at_original': primary['published_at'],
            'categories': sorted({c for r in members for c in r['categories']}),
            'event_ids': sorted({e for r in members for e in r['event_ids']}),
            'tier': tier_for(primary['publication_date'], as_of),
            'body_sha256': body_hash, 'source_evidence': primary['source_evidence'],
            'duplicate_sources': [{'news_id':'pub-'+r['article_id'], 'title':r['title'],
                'url':r['url'], 'publication_date':r['publication_date'],
                'body_sha256':r['source_evidence']['body_sha256']}
                for r in sorted(members, key=lambda r:r['url']) if r is not primary],
            'dedup_basis': 'byte_identical_body_only', 'relations': [],
        }
        articles.append(article)
    articles.sort(key=lambda r:r['news_id'])
    index = {r['news_id']:r for r in articles}
    event_members = defaultdict(list)
    for r in articles:
        for eid in r['event_ids']:
            event_members[eid].append(r)
    chains, edges = [], []
    for eid, members in sorted(event_members.items()):
        group = groups.get(eid)
        if not group or group['discovery_group_type'] == 'topic_collection':
            continue
        ordered = sorted(members, key=lambda r:(r['publication_date'],r['news_id']))
        chains.append({'event_id':eid, 'title':group['title'],
            'status':'discovery_membership_unreviewed',
            'nodes':[{'news_id':r['news_id'], 'publication_date':r['publication_date'],
                      'title':r['title'], 'url':r['url']} for r in ordered]})
        for left, right in zip(ordered, ordered[1:]):
            # A discovery group plus time order establishes no causal/semantic claim.
            kind = 'same_day_candidate' if left['publication_date'] == right['publication_date'] else 'chronological_next_candidate'
            edge = {'edge_id':'edge-'+digest([eid,left['news_id'],right['news_id']])[:24],
                'event_id':eid, 'event_title':group['title'],
                'source_news_id':left['news_id'], 'target_news_id':right['news_id'],
                'relation':kind, 'status':'unreviewed_candidate',
                'causal_claim':False, 'affects_ranking':False,
                'evidence':[{'news_id':r['news_id'], 'url':r['url'],
                    'publication_date':r['publication_date'], 'quote':r['body'][:200],
                    'body_sha256':r['body_sha256']} for r in (left,right)]}
            edges.append(edge)
            for here, other in ((left,right),(right,left)):
                index[here['news_id']]['relations'].append({**edge,
                    'related_news_id':other['news_id'], 'related_title':other['title'],
                    'related_url':other['url']})
    queries = []
    templates = ('{title}有哪些具体报道？', '{title}的进展与相关回应有哪些？', '{title}有哪些背景和细节报道？')
    for category in CATEGORIES:
        candidates = [g for g in groups.values() if g['category'] == category and event_members[g['event_id']]]
        # Prefer multi-source event leads, while keeping the category balanced.
        candidates.sort(key=lambda g:(g['discovery_group_type']=='topic_collection',
            len(event_members[g['event_id']]) < 2, -len(event_members[g['event_id']]),g['event_id']))
        for i, g in enumerate(candidates[:3]):
            text = templates[i].format(title=g['title'])
            queries.append({'query_id':'annual-'+digest([g['event_id'],text])[:20],
                'text':text, 'category':category, 'event_id_hint':g['event_id'],
                'query_source':'discovery_based_task_draft', 'relevance_labels':None,
                'as_of_date':as_of.isoformat(), 'limit':10})
    return articles, aliases, chains, edges, queries


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--corpus-dir',type=Path,required=True)
    p.add_argument('--discovery',type=Path,action='append',required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--as-of-date',default='2026-10-27')
    args=p.parse_args()
    if args.output_dir.exists():
        p.error('output exists; preserve the frozen experiment and select a new directory')
    as_of=date.fromisoformat(args.as_of_date)
    records, nonready, report=verify_records(args.corpus_dir)
    groups, sources, _, _=load_discovery(args.discovery)
    candidates, skipped=content_candidates(records,ROOT)
    if skipped or len(candidates)!=len(records):
        raise ValueError('content candidate conversion dropped verified sources')
    package=build_package(candidates,tenant_id='public-news-local')
    articles, aliases, chains, edges, queries=prepare_records(records,groups,as_of=as_of)
    output=args.output_dir
    output.mkdir(parents=True,exist_ok=False)
    save_package(package,output/'content-release',inventory={'corpus_report_sha256':sha256((args.corpus_dir/'report.json').read_bytes()).hexdigest()})
    engine=create_engine('sqlite:///'+str((output/'news.sqlite3').resolve()))
    store=FrozenNewsStore(engine)
    store.initialize()
    imported=store.import_release(package)
    engine.dispose()
    payloads={'news.jsonl':articles, 'queries.jsonl':queries, 'relations.jsonl':edges,
              'event-chains.jsonl':chains}
    files={}
    for name, rows in payloads.items():
        raw=''.join(canonical_json(row)+'\n' for row in rows).encode('utf-8')
        (output/name).write_bytes(raw)
        files[name]={'sha256':sha256(raw).hexdigest(),'rows':len(rows)}
    manifest={
        'schema_version':1,'corpus_version':'annual-'+digest({'release':package['manifest']['identity_sha256'],'policy':POLICY,'as_of':args.as_of_date,'files':files})[:24],
        'as_of_date':args.as_of_date,'tier_policy':{'version':POLICY,'basis':'publication_calendar_day_age','hot_max_age_days':7,'warm_max_age_days':90,'no_behavior_heat':True},
        'source_count':len(records),'canonical_count':len(articles),
        'exact_duplicate_sources_collapsed':len(records)-len(articles),
        'content_release':package['manifest'],'files':files,
        'category_counts':dict(Counter(c for r in articles for c in r['categories'])),
        'tier_counts':dict(Counter(r['tier'] for r in articles)),
        'query_count':len(queries),'relation_candidate_count':len(edges),
        'publication_time_missing':sum(r['publish_time'] is None for r in articles),
        'discovery_sources':[{'path':str(Path(s['path']).relative_to(ROOT)),'sha256':s['sha256']} for s in sources],
        'relevance_status':'pending_human', 'relations_status':'chronological_candidates_only',
        'warnings':['当前分层按公开新闻的发布日期计算，不含真实行为热度。',
            '事件归组与时间线是待核查线索，不证明因果，也不参与召回加分。',
            '人工标注前不报告语义准确率；未判定不能当作不相关。',
            '这是559条公开来源的冻结实验语料，板块和月份分布不均衡。'],
    }
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'corpus_version':manifest['corpus_version'],'sources':len(records),'canonical':len(articles),'queries':len(queries),'tiers':manifest['tier_counts'],'relations':len(edges),'database':str(output/'news.sqlite3'),'import_result':str(imported)},ensure_ascii=False))

if __name__=='__main__':
    main()
