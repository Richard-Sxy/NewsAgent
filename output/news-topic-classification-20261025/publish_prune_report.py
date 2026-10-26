"""Publish local human-readable results only after live acceptance succeeds."""
from collections import Counter
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path

root=Path(__file__).resolve().parents[2]
data=root/'var/news-storage/retained-ten-categories-20261025'
selection=json.loads((data/'selection.json').read_text())
applied=json.loads((data/'apply-result.json').read_text())
verified=json.loads((data/'live-verification.json').read_text())
assert applied['status']=='applied_verified' and verified['status']=='passed'
assert applied['operation_id']==verified['operation_id']==selection['operation_id']
assert not applied['validation_database_only']
decisions=[json.loads(line) for line in (data/'decisions.jsonl').read_text().splitlines()]
actions=Counter(r['action'] for r in decisions)
pointer={**selection,'status':'applied_verified','catalog_id':applied['catalog']['catalog_id'],
         'verified_at':datetime.now(timezone.utc).isoformat(),
         'content_path':str(data.relative_to(root)/'content'),
         'tags_path':str(data.relative_to(root)/'tags'),
         'export_path':str(data.relative_to(root)/'all-news-with-tags.jsonl'),
         'verification_path':str(data.relative_to(root)/'live-verification.json'),
         'backup_path':'var/backups/news-category-prune-20261025/unified-news.before.dump',
         'scope':applied['scope']}
current_path=root/'var/news-storage/current-selection.json'
current=json.loads(current_path.read_text()) if current_path.exists() else {}
if not (current.get('annotation_version') and current.get('content_release_id')==pointer['content_release_id']):
    current_path.write_text(json.dumps(pointer,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
rows=''.join(f'<tr><td>{escape(name)}</td><td>{count:,}</td></tr>' for name,count in selection['target_categories'].items())
samples=[]
for name in selection['target_categories']:
    items=[r for r in decisions if r['action']=='keep_target' and r['target_category_name']==name][:8]
    samples.append('<details><summary>'+escape(name)+' · 保留样本</summary><ul>'+''.join('<li>'+escape(r['display_title'])+' <code>'+escape(r['news_id'])+'</code></li>' for r in items)+'</ul></details>')
report=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>新闻清理结果 · 2026-10-25</title>
<style>body{{font:16px/1.7 system-ui,sans-serif;color:#172b4d;background:#f3f6fa;margin:0}}main{{max-width:980px;margin:auto;padding:36px 24px}}h1{{font-size:30px;margin-bottom:8px}}h2{{font-size:21px}}.cards{{display:flex;gap:14px;flex-wrap:wrap}}.card{{background:white;padding:22px;border-radius:12px;flex:1;min-width:180px}}strong{{font-size:32px;display:block;color:#17605a}}section{{background:white;border-radius:12px;padding:24px;margin-top:20px}}table{{border-collapse:collapse;width:100%}}td,th{{padding:7px 12px;border-bottom:1px solid #e4e9ef;text-align:left}}code{{font-size:12px;overflow-wrap:anywhere}}.muted{{color:#586980}}summary{{cursor:pointer;padding:8px}}a{{color:#145cb8}}</style>
<main><p class="muted">统一新闻库首次清理记录 · 已执行并验证 · 2026-10-25</p><h1>保留目标类别，暂留不确定新闻</h1><p>按用户确认的保守口径执行。以下记录首次清理时的分类统计。</p><p>后续自动补标的当前结果请查看 <a href="auto-annotations-report.html">4,714篇新闻自动标注报告</a>；原待复核记录无需用户人工补标。</p>
<div class="cards"><div class="card">当前保留<strong>16,369</strong>目标类别 11,655 + 待复核 4,714</div><div class="card">已删除新闻 ID<strong>5,066</strong>对应 5,159 个正文版本</div><div class="card">当前实时目录<strong>6,486</strong>满足既有正文、元数据及去重条件</div></div>
<section><h2>10 类目标新闻</h2><p class="muted">股票从财经、数码从科技中按标题证据细分。分类属于规则初稿，不是人工金标。</p><table><thead><tr><th>类别</th><th>保留篇数</th></tr></thead><tbody>{rows}</tbody><tfoot><tr><th>目标类别合计</th><th>11,655</th></tr></tfoot></table></section>
<section><h2>暂留待复核：4,714 篇</h2><ul><li>未分类：2,345 篇。</li><li>候选分数接近：1,280 篇。</li><li>主类在范围外，但次类涉及目标类别：1,089 篇。</li></ul><p>这些新闻仍可读取；没有将它们强制算进上述 10 类计数。</p></section>
<section><h2>实际验证</h2><ul><li>正文与标签各 16,369 条；保留 ID 和原正文哈希完全一致。</li><li>5,066 个剔除 ID 在正文版本表及两个实时可读视图中均为零。</li><li>涉及剔除新闻的 {applied['invalidated_historical_windows']:,} 个历史窗口已标记失效。</li><li>新目录已选中；动态采集与墙钟采集均保持暂停。</li><li>旧目录可以读取保留新闻，无法读取剔除新闻；两个新闻 API 检查通过。</li></ul>
<p>本次清理当前统一内容数据库和实时目录。原始采集缓存、旧冻结输入包、独立历史向量/数仓资料作为来源与恢复材料保留。</p></section>
<section><h2>保留新闻样本</h2>{''.join(samples)}</section>
<section><h2>数据与恢复入口</h2><p><a href="../../var/news-storage/retained-ten-categories-20261025/all-news-with-tags.jsonl">保留新闻全文与原标签</a> · <a href="../../var/news-storage/retained-ten-categories-20261025/decisions.jsonl">逐篇分类及保留/删除决策</a> · <a href="../../var/news-storage/retained-ten-categories-20261025/live-verification.json">实际验收结果</a> · <a href="../../agent/docs/news-category-prune-20261025.md">调用链与恢复说明</a></p>
<p>当前正文：<code>{escape(selection['content_release_id'])}</code><br>当前标签：<code>{escape(selection['tag_release_id'])}</code><br>当前实时目录：<code>{escape(pointer['catalog_id'])}</code></p>
<p>完整备份：<code>var/backups/news-category-prune-20261025/unified-news.before.dump</code>。当前入口清单：<code>var/news-storage/current-selection.json</code>。</p></section></main></html>'''
destination=Path(__file__).with_name('prune-report.html')
destination.write_text(report,encoding='utf8')
print(destination)
