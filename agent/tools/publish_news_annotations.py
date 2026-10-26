"""Validate, persist and report a complete versioned automatic annotation set."""
from __future__ import annotations
import argparse
from collections import Counter
from hashlib import sha256
from html import escape
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from annotate_uncertain_news import CATEGORIES,DATA,ROOT,TARGET,VERSION,inputs,read_rows,write_json
import news_annotation_policy

NAMES={key:name for key,name,_ in CATEGORIES}


def finalize(directory):
    selection,source=inputs()
    manifest=json.loads((directory/'inference-manifest.json').read_text())
    predictions=read_rows(directory/'model-predictions.jsonl')
    expected={r['news_id']:r for r in source}
    adjudication_path=directory/'assistant-adjudications.json'
    adjudications=json.loads(adjudication_path.read_text()) if adjudication_path.exists() else []
    overrides={r['news_id']:r for r in adjudications}
    if len(overrides)!=len(adjudications): raise ValueError('Duplicate assistant adjudication')
    for news_id,override in overrides.items():
        original=expected.get(news_id)
        if (original is None or override['record_sha256']!=original['record_sha256']
                or override['category'] not in NAMES or override['confidence'] not in {'medium','low'}
                or not override['reason'] or not override['evidence_quote']
                or override['evidence_quote'] not in (original['title']+'\n'+'\n'.join(original['excerpts']))):
            raise ValueError('Invalid assistant adjudication identity/evidence')
    if len(predictions)!=4714 or len({r['news_id'] for r in predictions})!=4714:
        raise ValueError('A complete, unique 4,714-article annotation set is required')
    if manifest['version']!=VERSION or manifest['source_release_id']!=selection['content_release_id']:
        raise ValueError('Annotation source/version mismatch')
    labels=[]
    for row in sorted(predictions,key=lambda r:r['news_id']):
        original=expected.get(row['news_id'])
        if original is None or any(row[k]!=original[k] for k in original):
            raise ValueError('Annotation changed source identity/evidence')
        if row['prompt_sha256']!=manifest['prompt_sha256'] or row['version']!=VERSION:
            raise ValueError('Mixed inference policies')
        model_category=row['primary_category']
        scores=row['category_scores']
        if model_category not in NAMES or not scores or any(k not in NAMES or not 0<=v<=1 for k,v in scores.items()):
            raise ValueError('Invalid category/score')
        if scores[model_category]!=max(scores.values()):
            raise ValueError('Primary category differs from highest score')
        category,rule,term=news_annotation_policy.decide(row)
        confidence='high' if scores[model_category]>=.80 and row['score_margin']>=.60 else 'medium' if scores[model_category]>=.50 and row['score_margin']>=.20 else 'low'
        if rule and category!=model_category: confidence='medium'
        override=overrides.get(row['news_id'])
        if override:
            category=override['category']
            confidence=override['confidence']
        if row['content_state']=='title_only' and confidence=='high': confidence='medium'
        if category=='insufficient': confidence='low'
        scope='target' if category in TARGET else 'insufficient' if category=='insufficient' else 'outside_target'
        labels.append({'tenant_id':selection['tenant_id'],'content_release_id':selection['content_release_id'],
                       'news_id':row['news_id'],'record_sha256':row['record_sha256'],
                       'primary_category':category,'primary_name':NAMES[category],
                       'target_category':category if category in TARGET else None,
                       'retention_scope':scope,'confidence':confidence,'annotation_status':'auto_labeled',
                       'annotation_version':VERSION,'human_verified':False,'manual_annotation_required':False,
                       'evidence':{'title':row['title'],'title_source':row['title_source'],'excerpts':row['excerpts'],
                                   'content_state':row['content_state'],'source_flags':row['source_flags'],
                                   'category_scores':scores,'score_margin':row['score_margin'],
                                   'model_primary_category':model_category,'boundary_rule':rule,'boundary_evidence':term,
                                   'assistant_adjudication':override,
                                   'confidence_basis':'uncalibrated model scores; title-only, editorial and assistant adjudications capped',
                                   'previous_action':row['previous_action'],'previous_primary':row['previous_primary']}})
    payload=''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in labels)
    output=directory/'annotations.jsonl'
    if output.exists() and output.read_text()!=payload: raise ValueError('Refusing to overwrite different annotations')
    output.write_text(payload,encoding='utf8')
    result={**manifest,'annotation_sha256':sha256(output.read_bytes()).hexdigest(),
            'annotation_method':'local_hunyuan_semantics_plus_editorial_boundaries_and_assistant_adjudication',
            'assistant_adjudication_count':len(overrides),
            'assistant_adjudication_sha256':sha256(adjudication_path.read_bytes()).hexdigest() if adjudications else None,
            'boundary_policy_version':news_annotation_policy.VERSION,
            'boundary_policy_sha256':sha256(Path(news_annotation_policy.__file__).read_bytes()).hexdigest(),
            'categories':dict(Counter(r['primary_name'] for r in labels)),
            'scope_counts':dict(Counter(r['retention_scope'] for r in labels)),
            'confidence_counts':dict(Counter(r['confidence'] for r in labels)),
            'manual_annotation_required':0,'deleted_news':0,
            'note':'Automatic semantic labels; no calibrated accuracy claim or content deletion.'}
    write_json(directory/'annotation-manifest.json',result)
    return result


def apply(directory,env,dry_run=False):
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    manifest=finalize(directory)
    labels=read_rows(directory/'annotations.jsonl')
    source=json.loads((DATA/'selection.json').read_text())
    with psycopg.connect(os.environ[env],row_factory=dict_row) as db:
        db.execute('SELECT pg_advisory_xact_lock(480025)')
        previous_view=db.execute("SELECT pg_get_viewdef('public.news_content_current_categories'::regclass,true) AS sql").fetchone()['sql']
        previous_path=directory/'previous-current-categories-view.sql'
        if not previous_path.exists():
            previous_path.write_text('CREATE OR REPLACE VIEW public.news_content_current_categories AS\n'+previous_view,encoding='utf8')
        before=db.execute('SELECT news_id,record_sha256 FROM news_content_selected WHERE tenant_id=%s AND release_id=%s ORDER BY news_id',(source['tenant_id'],source['content_release_id'])).fetchall()
        if len(before)!=16369: raise ValueError('Current collection changed')
        expected=db.execute("SELECT d.news_id,d.record_sha256 FROM news_content_prune_decisions d JOIN news_content_prune_runs p USING(operation_id) WHERE p.target_release_id=%s AND d.action LIKE 'hold_%%'",(source['content_release_id'],)).fetchall()
        if {r['news_id']:r['record_sha256'] for r in expected}!={r['news_id']:r['record_sha256'] for r in labels}:
            raise ValueError('Pending source IDs/hashes differ')
        # The original deletion audit remains immutable; semantic annotations are
        # a new explicitly selected version, not edits to pruning decisions.
        db.execute('''CREATE TABLE IF NOT EXISTS news_content_annotation_releases(
            tenant_id text NOT NULL, content_release_id text NOT NULL, annotation_version text NOT NULL,
            annotation_sha256 text NOT NULL, manifest jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(tenant_id,content_release_id,annotation_version))''')
        db.execute('''CREATE TABLE IF NOT EXISTS news_content_auto_annotations(
            tenant_id text NOT NULL, content_release_id text NOT NULL, annotation_version text NOT NULL,
            news_id text NOT NULL,record_sha256 text NOT NULL,primary_category text NOT NULL,
            primary_name text NOT NULL,target_category text,retention_scope text NOT NULL,
            confidence text NOT NULL,annotation_status text NOT NULL,evidence jsonb NOT NULL,
            PRIMARY KEY(tenant_id,content_release_id,annotation_version,news_id),
            FOREIGN KEY(tenant_id,content_release_id,annotation_version)
              REFERENCES news_content_annotation_releases)''')
        old=db.execute('SELECT annotation_sha256 FROM news_content_annotation_releases WHERE tenant_id=%s AND content_release_id=%s AND annotation_version=%s',(source['tenant_id'],source['content_release_id'],VERSION)).fetchone()
        if old and old['annotation_sha256']!=manifest['annotation_sha256']:
            raise ValueError('Existing annotation version differs')
        if not old:
            db.execute('INSERT INTO news_content_annotation_releases(tenant_id,content_release_id,annotation_version,annotation_sha256,manifest) VALUES(%s,%s,%s,%s,%s)',(source['tenant_id'],source['content_release_id'],VERSION,manifest['annotation_sha256'],Jsonb(manifest)))
            with db.cursor() as cur,cur.copy('COPY news_content_auto_annotations(tenant_id,content_release_id,annotation_version,news_id,record_sha256,primary_category,primary_name,target_category,retention_scope,confidence,annotation_status,evidence) FROM STDIN') as copy:
                for r in labels:
                    copy.write_row((r['tenant_id'],r['content_release_id'],VERSION,r['news_id'],r['record_sha256'],r['primary_category'],r['primary_name'],r['target_category'],r['retention_scope'],r['confidence'],r['annotation_status'],Jsonb(r['evidence'])))
        actual=db.execute('SELECT news_id,record_sha256,primary_category,primary_name,target_category,retention_scope,confidence,annotation_status,evidence FROM news_content_auto_annotations WHERE tenant_id=%s AND content_release_id=%s AND annotation_version=%s ORDER BY news_id',(source['tenant_id'],source['content_release_id'],VERSION)).fetchall()
        if actual!=[{k:r[k] for k in actual[0]} for r in sorted(labels,key=lambda r:r['news_id'])]:
            raise ValueError('Annotation full readback differs')
        # Retain the original 17 columns and types; append explicit annotation metadata.
        sql='''CREATE OR REPLACE VIEW news_content_current_categories AS
            SELECT c.*,COALESCE(a.target_category,d.target_category) AS target_category,
              COALESCE(a.annotation_status,d.action) AS category_status,d.review_flags,
              COALESCE(a.primary_category,d.target_category) AS primary_category,
              COALESCE(a.primary_name,({}::jsonb)->>d.target_category) AS primary_name,a.confidence AS annotation_confidence,
              COALESCE(a.retention_scope,'target') AS retention_scope,
              a.annotation_version,a.evidence AS annotation_evidence
            FROM news_content_selected c JOIN news_content_prune_runs p
              ON p.target_release_id=c.release_id AND p.manifest->>'tenant_id'=c.tenant_id
            JOIN news_content_prune_decisions d ON d.operation_id=p.operation_id
              AND d.news_id=c.news_id AND d.record_sha256=c.record_sha256
            LEFT JOIN news_content_auto_annotations a ON a.tenant_id=c.tenant_id
              AND a.content_release_id=c.release_id AND a.news_id=c.news_id
              AND a.record_sha256=c.record_sha256 AND a.annotation_version=%s
            WHERE d.action<>'remove_other' '''
        # PostgreSQL view definitions cannot contain server-side bind parameters.
        from psycopg import sql as pgsql
        db.execute(pgsql.SQL(sql.replace('%s','{}')).format(pgsql.Literal(json.dumps(NAMES,ensure_ascii=False)),pgsql.Literal(VERSION)))
        after=db.execute('SELECT news_id,record_sha256 FROM news_content_selected WHERE tenant_id=%s AND release_id=%s ORDER BY news_id',(source['tenant_id'],source['content_release_id'])).fetchall()
        if before!=after: raise ValueError('Content identity changed during annotation')
        counts=db.execute('SELECT category_status,count(*) AS count FROM news_content_current_categories GROUP BY 1 ORDER BY 1').fetchall()
        if {r['category_status']:r['count'] for r in counts}!={'auto_labeled':4714,'keep_target':11655}: raise ValueError('Incomplete visible annotation')
        result={'status':'validated_rolled_back' if dry_run else 'applied_verified','annotation_version':VERSION,'annotation_sha256':manifest['annotation_sha256'],
                'annotated_news':len(actual),'current_news':len(after),'content_hashes_unchanged':True,
                'visible_status_counts':counts,'remaining_manual_annotation':0,'deleted_news':0,
                'scope_counts':manifest['scope_counts'],'categories':manifest['categories']}
        if dry_run: db.rollback()
    write_json(directory/('dry-run-result.json' if dry_run else 'apply-result.json'),result)
    return result


def report(directory):
    directory=directory.resolve()
    result=json.loads((directory/'apply-result.json').read_text())
    manifest=json.loads((directory/'annotation-manifest.json').read_text())
    if result['status']!='applied_verified' or result['annotation_sha256']!=manifest['annotation_sha256']:
        raise ValueError('Only applied and verified annotations can be published')
    if sha256((directory/'annotations.jsonl').read_bytes()).hexdigest()!=manifest['annotation_sha256']:
        raise ValueError('Annotation content changed before report publication')
    labels=read_rows(directory/'annotations.jsonl')
    selection_path=ROOT/'var/news-storage/current-selection.json'
    selection=json.loads(selection_path.read_text())
    if selection['content_release_id']!=manifest['source_release_id']:
        raise ValueError('Current selection changed before report publication')
    backup_path=directory/'current-selection.before.json'
    if not backup_path.exists(): write_json(backup_path,selection)
    original=json.loads((DATA/'selection.json').read_text())
    current_categories=Counter(original['target_categories'])
    current_categories.update(r['primary_name'] for r in labels if r['retention_scope']=='target')
    # Keep the original deletion manifest immutable; update only the current pointer.
    selection.update(annotation_version=VERSION,annotation_manifest_path=str((directory/'annotation-manifest.json').relative_to(ROOT)),
                     annotations_path=str((directory/'annotations.jsonl').relative_to(ROOT)),
                     auto_annotated_previous_uncertain=4714,remaining_manual_annotation=0,
                     original_held_count=4714,current_target_count=11655+manifest['scope_counts'].get('target',0),
                     original_target_categories=original['target_categories'],target_categories=dict(current_categories),
                     annotation_scope_counts=manifest['scope_counts'])
    selection['held_for_review']=0
    temporary=selection_path.with_suffix('.annotation.tmp')
    write_json(temporary,selection)
    temporary.replace(selection_path)
    table=''.join('<tr><td>'+escape(name)+'</td><td>'+str(count)+'</td></tr>' for name,count in sorted(manifest['categories'].items(),key=lambda x:-x[1]))
    display=[{'id':r['news_id'],'title':r['evidence']['title'] or '原始标题缺失',
              'category':r['primary_name'],'scope':r['retention_scope'],'confidence':r['confidence'],
              'evidence':r['evidence']} for r in labels]
    embedded=json.dumps(display,ensure_ascii=False).replace('<','\\u003c')
    template=r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>不确定新闻自动标注结果</title>
<style>body{font:15px/1.7 system-ui,sans-serif;background:#f2f5f9;color:#21324b;margin:0}main{max-width:1180px;margin:auto;padding:30px 22px}section{background:white;border-radius:12px;padding:24px;margin:20px 0}h1{font-size:30px}h2{font-size:21px}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:9px;border-bottom:1px solid #e2e7ee;vertical-align:top}input,select,button{padding:9px;margin:4px;border:1px solid #ccd6e2;border-radius:6px;background:white}input{min-width:270px}summary{cursor:pointer}small,.note{color:#63738b}code{font-size:12px;overflow-wrap:anywhere}.cards{display:flex;gap:16px;flex-wrap:wrap}.card{background:#eaf4f3;padding:18px 24px;border-radius:8px;flex:1;min-width:170px}.card strong{display:block;font-size:30px;color:#14645b}p{margin:10px 0}.evidence{white-space:pre-wrap;max-width:750px}</style>
<main><p class="note">NewsAgent · 本地自动语义标注 · 2026-10-25</p><h1>4,714 篇原待复核新闻已补标</h1><p>无需人工补标。每篇已有自动主类别、候选分数、证据和置信等级。低置信及信息不足明确保留，不会伪装成人工确认结果。</p>
<div class="cards"><div class="card">已自动标注<strong>4,714</strong>原待复核记录全部覆盖</div><div class="card">新增目标10类<strong>__TARGET__</strong>当前目标类别共 __CURRENT_TARGET__ 篇</div><div class="card">当前新闻总数<strong>16,369</strong>本轮未继续删除新闻</div></div>
<section><h2>标注分布</h2><p>目标范围 __TARGET__ 篇 · 范围外 __OUTSIDE__ 篇 · 综合/信息不足 __INSUFFICIENT__ 篇。范围外新闻仍保留在当前库。</p><table><tr><th>本轮自动主类</th><th>篇数</th></tr>__TABLE__</table><p class="note">置信等级是未校准的模型相对判断，不能理解为准确率；只含标题或经类别边界规则调整的结果最高标为中。原文、正文哈希、原标签冻结包及历史删除审计保持不变。</p></section>
<section><h2>逐篇查看</h2><input id="query" placeholder="搜索标题或新闻 ID"><select id="category"><option value="">全部类别</option></select><select id="confidence"><option value="">全部置信等级</option><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select><p id="status"></p><table><thead><tr><th>新闻与证据</th><th>主类</th><th>范围</th><th>置信</th></tr></thead><tbody id="rows"></tbody></table><button id="previous">上一页</button><button id="next">下一页</button></section>
<section><h2>方法与数据</h2><p>本地 Hunyuan-7B-Instruct（固定版本、NF4推理）读取标题与独立文本片段 → 受限类别判别 → 版本化边界规则处理电竞/体育、海外社会、科技应用等交叉主题 → AI助手对残余明确主题逐篇补标 → 完整校验后写入自动标注表和当前分类视图。</p><p>从 <code>news_content_current_categories</code> 查询：<code>primary_category / primary_name / annotation_confidence / retention_scope / category_status</code>。本轮记录状态统一为 <code>auto_labeled</code>。</p><p>逐篇机器数据：<code>var/news-storage/uncertain-annotations-20261025/full-v2/annotations.jsonl</code>；配置、测试、恢复和局限见 <code>agent/docs/news-auto-annotations-20261025.md</code>。</p></section></main>
<script type="application/json" id="data">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent),q=document.getElementById('query'),cat=document.getElementById('category'),conf=document.getElementById('confidence');let page=0,filtered=[];const size=50,names={high:'高',medium:'中',low:'低'},scopes={target:'目标10类',outside_target:'范围外',insufficient:'信息不足'};
[...new Set(data.map(r=>r.category))].sort().forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;cat.append(o)});
function draw(){filtered=data.filter(r=>(!cat.value||r.category===cat.value)&&(!conf.value||r.confidence===conf.value)&&(!q.value||(r.title+' '+r.id).includes(q.value)));page=Math.min(page,Math.max(0,Math.ceil(filtered.length/size)-1));document.getElementById('status').textContent=`共 ${filtered.length} 篇 · 第 ${page+1} / ${Math.max(1,Math.ceil(filtered.length/size))} 页`;const body=document.getElementById('rows');body.replaceChildren();filtered.slice(page*size,(page+1)*size).forEach(r=>{const tr=document.createElement('tr'),td=document.createElement('td'),d=document.createElement('details'),s=document.createElement('summary'),p=document.createElement('div');s.textContent=r.title;p.className='evidence';p.textContent=r.id+'\n'+r.evidence.excerpts.join('\n——独立片段——\n')+'\n模型原主类：'+r.evidence.model_primary_category+'\n边界规则：'+(r.evidence.boundary_rule||'无')+'\n边界证据：'+(r.evidence.boundary_evidence||'无')+'\n助手补标理由：'+(r.evidence.assistant_adjudication?.reason||'无')+'\n候选相对分数（非准确率）：'+JSON.stringify(r.evidence.category_scores);d.append(s,p);td.append(d);tr.append(td);[r.category,scopes[r.scope],names[r.confidence]].forEach(v=>{const c=document.createElement('td');c.textContent=v;tr.append(c)});body.append(tr)});document.getElementById('previous').disabled=page===0;document.getElementById('next').disabled=(page+1)*size>=filtered.length}
[q,cat,conf].forEach(e=>e.addEventListener('input',()=>{page=0;draw()}));document.getElementById('previous').onclick=()=>{page--;draw()};document.getElementById('next').onclick=()=>{page++;draw()};draw();</script></html>'''
    replacements={'__TARGET__':str(manifest['scope_counts'].get('target',0)),
                  '__CURRENT_TARGET__':str(selection['current_target_count']),
                  '__OUTSIDE__':str(manifest['scope_counts'].get('outside_target',0)),
                  '__INSUFFICIENT__':str(manifest['scope_counts'].get('insufficient',0)),
                  '__TABLE__':table,'__DATA__':embedded}
    for key,value in replacements.items(): template=template.replace(key,value)
    destination=ROOT/'output/news-topic-classification-20261025/auto-annotations-report.html'
    destination.write_text(template,encoding='utf8')
    return {'report':str(destination),'current_target_count':selection['current_target_count']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['finalize','apply','report'])
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--database-env',default='LAB_UNIFIED_DSN')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    value=finalize(args.directory) if args.command=='finalize' else report(args.directory) if args.command=='report' else apply(args.directory,args.database_env,args.dry_run)
    print(json.dumps(value,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
