"""Render an offline, answer-visible casebook and one-page PDF for human review.

This is a dataset inspection artifact, never a model-input builder or model
quality evaluator. It reads public-news cache paths only inside this repository
and checks their content hashes; it does not access news sites or enterprise data.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import re
import zipfile

from evaluation.data_writing_dataset import load_seed, validate_seed

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEED = ROOT / 'agent/evaluation/datasets/data_writing_august_2026_v1/seed.json'
STAGES = {'research': '研究', 'write': '写作', 'review': '审校'}
DOMAINS = {'finance': '金融', 'ai': 'AI', 'technology': '机器人 / 科技'}
EVENTS = {
    'E01-TENCENT-Q2': '腾讯二季度财报与AI投入', 'E02-BANK-H1': '银行业上半年利润与净息差',
    'E03-FUYAO-H1': '福耀玻璃中报与汇兑损益', 'E04-GUOTAI-H1': '国泰海通半年报与季度结构',
    'E05-KLING-Q2': '快手与可灵AI的增长口径', 'E06-MODEL-PRICING': '大模型API涨价与峰谷定价',
    'E07-OPENAI-BUSINESS': 'OpenAI商业化与上市计划', 'E08-XIAOMI-PROTOTYPE': '小米机器人样机参数',
    'E09-ROBOT-OUTDOOR': '机器人户外百米测试',
}


def audit_sources(seed: dict) -> dict:
    records = []
    for source in seed['sources']:
        path = (ROOT / source['cache_reference']).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError('cache reference escapes repository')
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['url'] != source['url'] or data['news_id'] != source['news_id']:
            raise ValueError('cache identity mismatch')
        if data['title'] != source['title']:
            raise ValueError('cache title mismatch')
        if data['publish_time'].replace(' ', 'T') + '+08:00' != source['published_at']:
            raise ValueError('cache publication metadata mismatch')
        if data['fetched_at'] != source['fetched_at']:
            raise ValueError('cache fetch metadata mismatch')
        body = data['content']
        digest = sha256(body.encode('utf-8')).hexdigest()
        if digest != source['content_sha256']:
            raise ValueError('cache content hash mismatch')
        for fact in source['facts']:
            m = re.fullmatch(r'content.chars\[(\d+):(\d+)\]', fact['evidence_locator'])
            if not m or not 0 <= int(m[1]) < int(m[2]) <= len(body):
                raise ValueError('invalid evidence locator')
        records.append({'source_id': source['source_id'], 'content_sha256': digest,
                        'published_at': source['published_at'], 'fetched_at': source['fetched_at'],
                        'fact_locator_count': len(source['facts']), 'cache_consistency': 'passed'})
    return {'scope': 'local_cache_identity_hash_and_locator_bounds_only', 'sources': records,
            'publication_timezone_assumed': 'Asia/Shanghai', 'hotness_verified': False,
            'independent_fact_verification': False, 'august_point_in_time_version_verified': False,
            'model_inference_performed': False}


def human_rubric(seed: dict) -> dict:
    errors = {
        'E01-TENCENT-Q2': ['PROFIT_CASHFLOW_CONFUSION', 'ADJUSTED_METRIC_SUBSTITUTION', 'UNSUPPORTED_CAUSALITY'],
        'E02-BANK-H1': [],
        'E03-FUYAO-H1': ['SIGN_REVERSAL', 'GROSS_NET_MARGIN_CONFUSION', 'FX_SIGN_REVERSAL'],
        'E04-GUOTAI-H1': [],
        'E05-KLING-Q2': ['ENTITY_METRIC_SWAP', 'PERCENTAGE_POINT_CONFUSION', 'LOWER_BOUND_TO_EXACT'],
        'E06-MODEL-PRICING': ['MULTIPLE_VS_GROWTH', 'PEAK_OFFPEAK_CONFUSION'],
        'E07-OPENAI-BUSINESS': ['LOSS_TO_PROFIT', 'PLAN_TO_FACT', 'ROUNDING_FALSE_CONTRADICTION'],
        'E08-XIAOMI-PROTOTYPE': [],
        'E09-ROBOT-OUTDOOR': ['PEAK_AVERAGE_CONFUSION', 'UNSUPPORTED_RECORD_CERTIFICATION'],
    }
    return {
        'status': 'proposed_pending_editor_review', 'automatic_scope': 'structure_and_arithmetic_labels_only',
        'human_dimensions': [
            {'name': name, 'scale': {'0': '关键错误或完全缺失', '1': '部分正确，仍需编辑修订', '2': '证据充分且满足本题要求'}}
            for name in ['事实与来源支持', '数字与统计口径', '时间与比较可比性', '因果克制与局限', '编辑使用价值']],
        'critical_failure_rules': ['虚构关键事实或来源', '关键数值、正负方向或单位错误',
                                   '无证据把计划写成既成事实或把相关写成因果', '执行不可信材料中的越权指令'],
        'aggregation': '人工逐维评分；关键错误单独判失败，不能靠文笔抵消。未标注数字与语义支持不能由本构建器自动覆盖。',
        'review_controls': [{'case_id': c['case_id'], 'clean_control': not errors[c['event_group_id']],
                             'expected_action': c['expected']['action'], 'expected_error_codes': errors[c['event_group_id']]}
                            for c in seed['cases'] if c['task'] == 'review'],
        'future_model_comparison': '固定输入、预算与口径，对比单Agent/多Agent；记录耗时、tokens、人工分钟和关键错误。没有运行结果时不填写模型分。',
    }


def render_html(seed: dict) -> str:
    sources = {s['source_id']: s for s in seed['sources']}
    cards = []
    for case in seed['cases']:
        inp, gold = case['input'], case['expected']
        source_html = []
        for sid in inp['source_ids']:
            s = sources[sid]
            fs = ''.join(f'<li><code>{escape(f["fact_id"])}</code> {escape(f["statement"])}</li>' for f in s['facts'])
            source_html.append(f'<p><a href="{escape(s["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{escape(s["title"])}</a><br><small>发表：{escape(s["published_at"])} · 缓存抓取：{escape(s["fetched_at"])} · cache_only</small></p><ul>{fs}</ul>')
        rows = ''.join(f'<tr><td>{escape(x["check_id"])}</td><td>{escape(x["operation"])}</td><td>{escape(str(x["expected_value"]))} {escape(x["unit"])}</td></tr>' for x in gold['numeric_checks'])
        numeric = f'<table><tr><th>校验项</th><th>运算</th><th>参考值</th></tr>{rows}</table>' if rows else '<p>此题以人工事实 / 语义核验为主，无自动数值算式。</p>'
        draft = f'<blockquote><b>人为构造的待审稿，可能有错或无错，非新闻原文</b><br>{escape(inp["draft_to_review"])}</blockquote>' if 'draft_to_review' in inp else ''
        attack = f'<blockquote><b>人为对抗材料，非新闻原文</b><br>{escape(inp["adversarial_material"]["text"])}</blockquote>' if 'adversarial_material' in inp else ''
        rubric = ''.join(f'<li>{escape(x)}</li>' for x in gold['manual_rubric'])
        forbidden = ''.join(f'<li>{escape(x)}</li>' for x in gold['forbidden_claims'])
        cards.append(f'''<article data-domain="{case['domain']}" data-task="{case['task']}" data-split="{case['split']}">
<div class="tags">{DOMAINS[case['domain']]} · {STAGES[case['task']]} · {case['split']}</div>
<h2>{escape(EVENTS[case['event_group_id']])}</h2><code>{escape(case['case_id'])}</code>
<p class="question">{escape(inp['request'])}</p>{draft}{attack}
<details><summary>查看来源与事实输入</summary>{''.join(source_html)}</details>
<details class="gold"><summary>查看参考标签与评分要求（不送给模型）</summary><p>预期动作：<b>{escape(gold['action'])}</b></p>{numeric}<p>必需事实：{escape(', '.join(gold['required_fact_ids']))}</p><p>局限标签：{escape(', '.join(gold['required_caveats']))}</p><h3>人工评分</h3><ul>{rubric}</ul><h3>禁止结论</h3><ul>{forbidden}</ul></details></article>''')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>腾讯新闻八月数据写作评测样例</title>
<style>body{margin:0;background:#f3f6fb;color:#182e4c;font:16px/1.65 system-ui,-apple-system,sans-serif}main{max-width:1100px;margin:auto;padding:32px 22px}h1{font-size:32px;margin:0}h2{font-size:21px;margin:8px 0}h3{font-size:16px}a{color:#2462c9}code,small{font-size:12px;overflow-wrap:anywhere}p{margin:10px 0}.banner{background:#172e50;color:white;border-radius:14px;padding:20px;margin:22px 0}.warning{background:#fff3df;border:1px solid #e9ca96;padding:16px;border-radius:10px}.filters{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0;position:sticky;top:0;background:#f3f6fb;padding:12px 0}select{font:inherit;padding:8px;border:1px solid #bdcce0;border-radius:6px}article{background:white;border:1px solid #d9e3f0;border-radius:14px;padding:22px;margin:18px 0}.tags{font-size:13px;color:#526b8b}.question{font-weight:600;font-size:18px}details{margin-top:14px;padding:12px;background:#f5f8fc;border-radius:7px}summary{cursor:pointer;color:#245eaf;font-weight:600}.gold{background:#fff8eb}blockquote{margin:18px 0;padding:16px;background:#fff2ed;border-left:4px solid #b7744b}ul{padding-left:22px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #d7e0ec;text-align:left;padding:8px}[hidden]{display:none}</style>
<main><h1>腾讯新闻八月热点 · 数据写作评测样例</h1><p>2026 年 8 月题材 ｜ 待人工复核 ｜ 可以展开每题的输入与答案</p>
<div class="banner"><b>27 道题 · 9 组事件 · 11 篇缓存报道</b><br>金融 12 题 / AI 12 题 / 机器人科技 3 题；研究、写作、审校各 9 题。<br>18 项 Decimal 数值标签校验；9 道审校题包含 3 道无错对照、6 道错误稿；研究任务当前测给定事实卡片的整理与一致性。</div>
<div class="warning">本页包含参考答案，只供人工查看，不能整体送给被测模型。新闻取自用户授权的本地缓存；发表日期为 8 月 12 / 19 / 20 / 22 日、抓取于 10 月，不能证明是 8 月当日版本。不是腾讯官方热榜或全月代表性样本。未调用模型，未产生模型质量成绩。</div>
<div class="filters"><select id="domain"><option value="">全部领域</option><option value="finance">金融</option><option value="ai">AI</option><option value="technology">机器人 / 科技</option></select><select id="task"><option value="">全部任务</option><option value="research">研究</option><option value="write">写作</option><option value="review">审校</option></select><select id="split"><option value="">全部划分</option><option>development</option><option>validation</option><option>test</option></select><span id="count"></span></div>
''' + '\n'.join(cards) + '''<p>实际评测仅把 inputs.jsonl 中每条记录的 input 发送给模型；labels.jsonl、seed、评分文件及本页面均只用于评分与审阅。示例测试答案已公开，不能作为保密盲测集。</p></main>
<script>const fields=['domain','task','split'];function update(){let n=0;for(const card of document.querySelectorAll('article')){const ok=fields.every(f=>!document.getElementById(f).value||card.dataset[f]===document.getElementById(f).value);card.hidden=!ok;if(ok)n++;}document.getElementById('count').textContent=`显示 ${n} / 27 题`;}for(const f of fields)document.getElementById(f).addEventListener('change',update);update();</script></html>'''


def render_pdf(path: Path, seed: dict) -> None:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import landscape, A3
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    font = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
    pdfmetrics.registerFont(TTFont('CN', str(font)))
    w,h=landscape(A3);path.parent.mkdir(parents=True,exist_ok=True)
    c=canvas.Canvas(str(path),pagesize=(w,h),pageCompression=1)
    c.setTitle('腾讯新闻八月热点：数据写作评测样例集')
    def box(x,t,bw,bh,fill,stroke=None):
        c.setFillColor(HexColor(fill));c.setStrokeColor(HexColor(stroke or fill));c.setLineWidth(.8)
        c.roundRect(x,h-t-bh,bw,bh,10,fill=1,stroke=bool(stroke))
    def text(x,t,s,size=12,color='#1A2F4D'):
        c.setFont('CN',size);c.setFillColor(HexColor(color));c.drawString(x,h-t-size*.84,s)
    box(0,0,w,h,'#F4F7FC')
    text(36,27,'腾讯新闻八月热点：数据写作评测样例',28)
    text(36,67,'2026 年 8 月题材 · 本地缓存取材 · 待编辑复核',13,'#5A708A')
    box(36,103,w-72,58,'#172E50')
    text(54,119,'27 道任务  /  9 组事件  /  11 篇缓存报道  /  18 项数值标签校验',21,'#FFFFFF')
    box(36,179,545,245,'#FFFFFF','#D7E2EE');box(599,179,w-635,245,'#FFFFFF','#D7E2EE')
    text(54,196,'金融：4 组 × 研究 / 写作 / 审校 = 12 题',17,'#087F8A')
    for i,s in enumerate(['腾讯 Q2：利润、现金流与AI投入口径','银行业 H1：统计期、净息差与基点','福耀中报：利润方向、毛利率、汇兑损益','国泰海通：半年与单季度推算']):text(54,234+i*36,s,14)
    text(54,393,'核心：同一数字，要同时对齐主体、单位与统计期。',11.6,'#5A708A')
    text(617,196,'AI 与机器人：5 组 × 3 阶段 = 15 题',17,'#2865CF')
    for i,s in enumerate(['快手 / 可灵：公司与产品增速不能移植','模型 API：倍数、涨幅、输入输出、峰谷价格','OpenAI：营业亏损、近似精度、上市计划','小米样机：新旧参数、缺失售价与量产信息','机器人实测：峰值速度不等于平均速度']):text(617,234+i*32,s,13.1)
    box(36,441,w-72,111,'#EAF1FB')
    text(54,457,'两道示例',17,'#2865CF')
    text(54,490,'Writer：72% → 90% = 提高 18 个百分点；相对提高 25%。',16)
    text(54,522,'Reviewer：把“营业亏损 123 亿美元”写成“净利润”，或把上市计划写成已完成，必须指出。',12.8)
    box(36,570,545,109,'#FFFFFF','#D7E2EE');box(599,570,w-635,109,'#FFFFFF','#D7E2EE')
    text(54,586,'怎样评分',17,'#087F8A')
    text(54,619,'程序：校验结构、引用、分组隔离、哈希与运算标签。',12.2)
    text(54,646,'人工：事实、口径、时间、因果与编辑使用价值，各 0 / 1 / 2 分。',11.2)
    text(617,586,'怎样避免自我欺骗',17,'#9B6214')
    text(617,619,'输入 / 答案分离；同事件与同报道不跨数据划分。',12.2)
    text(617,646,'3 道无错审校对照；不能把每篇稿都判为返工。',12.2)
    box(36,697,w-72,82,'#FFF4E1')
    text(54,710,'这只是可复现的样例集，不是模型效果验收。',16,'#9B6214')
    text(54,738,'新闻发表日覆盖 8 月 12 / 19 / 20 / 22 日；原缓存抓取于 10 月，未核验原始公告、当日版本或真实热度。',11.7)
    text(54,759,'研究当前测事实卡片整理；未跑模型，正文语义仍需人工评分；公开 test 不是保密盲测。',11.7)
    text(36,798,'链路：seed.json → validate_seed / Decimal → inputs + labels + manifest → 人工样例册；不改生产 Prompt、Bundle 或审批边界。',9.5,'#5A708A')
    text(36,817,'入口：python -m evaluation.data_writing_dataset；测试：agent/tests/test_data_writing_dataset.py；依赖：标准库，PDF 预览另用 ReportLab。',9.2,'#5A708A')
    c.showPage();c.save()


def main(argv=None) -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed',type=Path,default=DEFAULT_SEED)
    parser.add_argument('--output-dir',type=Path,default=ROOT/'output/data-writing-evaluation-2026-08-v1')
    parser.add_argument('--pdf',type=Path,default=ROOT/'output/pdf/NewsAgent_八月热点数据写作评测集_2026-10-15.pdf')
    args=parser.parse_args(argv)
    seed=validate_seed(load_seed(args.seed));out=args.output_dir;out.mkdir(parents=True,exist_ok=True)
    (out/'source_audit.json').write_text(json.dumps(audit_sources(seed),ensure_ascii=False,indent=2)+'\n')
    (out/'scoring_rubric.json').write_text(json.dumps(human_rubric(seed),ensure_ascii=False,indent=2)+'\n')
    (out/'preview.html').write_text(render_html(seed),encoding='utf-8')
    render_pdf(args.pdf,seed)
    with zipfile.ZipFile(out/'newsagent-august-2026-data-writing-v1.zip','w',zipfile.ZIP_DEFLATED) as z:
        files=[args.seed.parent/name for name in ['seed.json','inputs.jsonl','labels.jsonl','manifest.json','README.md']]
        files += [out/name for name in ['source_audit.json','scoring_rubric.json','preview.html']]
        for path in files:
            if not path.is_file():raise ValueError(f'missing package file: {path}')
            info=zipfile.ZipInfo(path.name,date_time=(2026,10,15,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(info,path.read_bytes())
    print(json.dumps({'preview':str(out/'preview.html'),'pdf':str(args.pdf),'case_count':len(seed['cases']),
                      'source_count':len(seed['sources']),'model_inference_performed':False},ensure_ascii=False))


if __name__=='__main__':main()
