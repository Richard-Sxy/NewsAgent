"""Build the current, code-backed NewsAgent field dictionary (no schema changes)."""
from pathlib import Path
from xml.sax.saxutils import escape
import hashlib
import json

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, PageBreak, Preformatted
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'output/pdf/NewsAgent_当前新闻与用户标签_字段手册.pdf'
QA = ROOT / 'tmp/pdfs/current-schema'
QA.mkdir(parents=True, exist_ok=True)
pdfmetrics.registerFont(TTFont('CN', 'C:/Windows/Fonts/msyh.ttc', subfontIndex=0))
pdfmetrics.registerFont(TTFont('CNB', 'C:/Windows/Fonts/msyhbd.ttc', subfontIndex=0))
NAVY = colors.HexColor('#163249')
TEAL = colors.HexColor('#007F83')
GRAY = colors.HexColor('#536676')
W = A4[0] - 64
ST = {
 'title': ParagraphStyle('title', fontName='CNB', fontSize=18, leading=24, textColor=NAVY, spaceAfter=7),
 'sub': ParagraphStyle('sub', fontName='CNB', fontSize=10, leading=14, textColor=TEAL, spaceBefore=7, spaceAfter=4),
 'body': ParagraphStyle('body', fontName='CN', fontSize=8.3, leading=12, textColor=NAVY, spaceAfter=6),
 'small': ParagraphStyle('small', fontName='CN', fontSize=7.5, leading=10.6, textColor=GRAY, spaceAfter=5),
 'cell': ParagraphStyle('cell', fontName='CN', fontSize=8, leading=10.8, textColor=NAVY, wordWrap='CJK'),
 'head': ParagraphStyle('head', fontName='CNB', fontSize=8, leading=11, textColor=colors.white),
 'code': ParagraphStyle('code', fontName='CN', fontSize=7.7, leading=11.2, textColor=NAVY, leftIndent=8, spaceAfter=8),
}
story = []
page_titles = []
documented_fields = {}
def p(s, style='body'):
    return Paragraph(escape(str(s)).replace('\n','<br/>'), ST[style])
def para(s, style='body'):
    story.append(p(s, style))
def page(title, summary):
    if story: story.append(PageBreak())
    page_titles.append(title)
    para(title, 'title')
    para(summary)
def sub(s): para(s, 'sub')
def table(headers, rows, widths):
    data = [[p(v,'head') for v in headers]] + [[p(v,'cell') for v in row] for row in rows]
    t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
    t.setStyle(TableStyle([
      ('BACKGROUND',(0,0),(-1,0),NAVY),('VALIGN',(0,0),(-1,-1),'TOP'),
      ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
      ('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3),
      ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F1F6F8')]),
      ('LINEBELOW',(0,0),(-1,0),0.7,TEAL),
      ('LINEBELOW',(0,1),(-1,-1),0.25,colors.HexColor('#DBE5EA')),
    ]))
    story.extend([t,Spacer(1,5)])
def fields(name, rows, key):
    sub(name)
    para(key,'small')
    documented_fields[name]=[r[0] for r in rows]
    table(['字段名','SQL 类型','可空','含义 / 当前约束'],rows,[172,94,29,W-295])
def code(s):
    for line in s.strip().splitlines():
        assert pdfmetrics.stringWidth(line,'CN',7.7) < W-16, line
    story.append(Preformatted(s.strip(),ST['code']))

page('01  热点新闻指标', '当前热点 SQL 读取字段。每行 = 租户 + 新闻 + 一个小时；所有时间窗口均为左闭右开。')
fields('dw.news_behavior_aggregate（视图）',[
 ('tenant_id','uuid','否','租户 ID；查询必须限定'),
 ('news_id','text','否','新闻 ID'),
 ('event_time','timestamptz','否','统计小时的起点'),
 ('title','text','否','新闻标题'),
 ('content_type','text','否','article 图文 / video 视频'),
 ('category','text','否','新闻栏目'),
 ('source','text','否','新闻内容来源'),
 ('publish_time','timestamptz','否','新闻发布时间'),
 ('impressions','bigint','否','曝光次数，≥ 0'),
 ('clicks','bigint','否','点击次数，0 到 impressions'),
 ('unique_users','bigint','否','该新闻、该小时点击去重人数，0 到 clicks'),
 ('total_duration_seconds','numeric(20,2)','否','累计消费时长，单位秒，≥ 0'),
 ('effective_consumptions','bigint','否','有效消费次数，0 到 clicks'),
 ('interactions','bigint','否','互动次数，≥ 0'),
 ('baseline_impressions','numeric(20,2)','是','同口径历史曝光基线'),
 ('baseline_clicks','numeric(20,2)','是','同口径历史点击基线'),
 ('baseline_effective_consumptions','numeric(20,2)','是','同口径历史有效消费基线'),
 ('baseline_interactions','numeric(20,2)','是','同口径历史互动基线'),
], '逻辑唯一键：(tenant_id, news_id, event_time)。视图本身没有物理主键。')
sub('对应物理表')
table(['表名（dw Schema）','主键','保存的字段'],[
 ('dim_news','tenant_id + news_id','新闻 ID、标题、类型、栏目、来源、发布时间'),
 ('news_metric_hourly','tenant_id + news_id + event_time','6 项行为指标：曝光、点击、UV、时长、有效消费、互动'),
 ('news_metric_baseline_hourly','tenant_id + news_id + event_time','4 项 baseline_* 基线字段'),
],[164,149,W-313])
para('三张表按主键关联；基线使用 LEFT JOIN，缺失时为 NULL。UV 不能跨小时直接相加；点击 UV 与后文的详情读者人数是不同统计口径。')
para('本文依据当前工作区源码，SQL 使用 PostgreSQL 方言；不包含企业未实现字段。本地行为与读者数据为模拟聚合。','small')

page('02  新闻正文与内容标签', '正文和标签分别存储；发布 ID 与内容哈希用于精确关联同一份内容。未列出的内部审计表不影响本页字段释义。')
fields('public.news_content_versions',[
 ('tenant_id','varchar(128)','否','内容域租户 ID'),
 ('news_id','varchar(160)','否','新闻 ID'),
 ('record_sha256','varchar(64)','否','完整标准化新闻记录的 SHA-256'),
 ('title','text','是','新闻标题'),
 ('body','text','是','新闻正文；没有全文时保持 NULL'),
 ('url','text','是','新闻原始链接'),
 ('publish_time','timestamptz','是','新闻发布时间'),
 ('content_type','varchar(16)','否','内容类型，article / video'),
 ('content_state','varchar(24)','否','body_available / fragments_only / title_only'),
 ('body_sha256','varchar(64)','是','正文 SHA-256；与整条记录哈希区分'),
 ('record','jsonb','否','扩展元数据、片段和质量标记'),
], '主键：(tenant_id, news_id, record_sha256)。读取选定内容使用 public.news_content_selected。')
fields('public.news_content_tags',[
 ('tenant_id','varchar(128)','否','内容域租户 ID'),
 ('content_release_id','varchar(160)','否','正文发布 ID'),
 ('tag_release_id','varchar(160)','否','标签发布 ID'),
 ('news_id','varchar(160)','否','新闻 ID'),
 ('record_sha256','varchar(64)','否','所标注的新闻记录 SHA-256'),
 ('body_sha256','varchar(64)','是','所标注的正文 SHA-256'),
 ('label_sha256','varchar(64)','否','标签结果摘要'),
 ('label_status','varchar(32)','否','tagged / unclassified / insufficient_input / metadata_review'),
 ('input_scope','varchar(24)','否','title_body / title_only / none'),
 ('provenance','varchar(32)','否','标签来源标识'),
 ('topic_ids','jsonb','否','该新闻的主题 ID 数组'),
 ('label','jsonb','否','完整标签结果，含规则命中与证据'),
], '主键：(tenant_id, content_release_id, tag_release_id, news_id)。外键绑定标签发布、正文选定项和内容记录。')
para('内容标签回答“这条新闻讲什么”。用户兴趣标签回答“这条新闻的读者平时关注什么”，两者不互相替代。','small')

page('03  用户标签：读者人数与兴趣主题', 'news_audience 保存新闻受众聚合。短期为 24 小时，长期为 720 小时；一条新闻会分别保存两种兴趣期限。')
fields('news_audience.news_reader_population',[
 ('tenant_id','uuid','否','业务租户 ID'),
 ('snapshot_id','text','否','受众快照 ID'),
 ('news_id','text','否','新闻 ID'),
 ('lookback_hours','integer','否','兴趣回看期限：24 / 720'),
 ('content_sha256','text','是','该新闻的正文 SHA-256'),
 ('source_has_body_hash','boolean','否','等于 content_sha256 是否非空'),
 ('reader_count','bigint','否','统计窗口内详情页去重读者数，≥ 0'),
 ('profiled_reader_count','bigint','否','有已知兴趣的读者数，≤ reader_count'),
 ('unknown_reader_count','bigint','否','reader_count - profiled_reader_count'),
 ('cold_start_reader_count','bigint','否','冷启动读者数，≤ unknown_reader_count'),
 ('baseline_profiled_count','bigint','否','同口径基准有画像人数，≥ 本篇已画像人数'),
 ('status','text','否','0 人为 empty；大于 0 人为 complete'),
 ('trigger_article_excluded','boolean','否','必须为 true：兴趣历史排除当前新闻'),
 ('limitations','text[]','否','限制说明，默认空数组'),
 ('source_payload_sha256','text','否','源聚合载荷 SHA-256，64 位小写十六进制'),
 ('source_topic_limit','integer','否','源输出主题数上限，1 到 50'),
 ('topics_are_top_k','boolean','否','是否只保留部分排名靠前的主题'),
 ('source_topic_count','integer','否','实际保留主题数，0 到 source_topic_limit'),
], '主键：(tenant_id, snapshot_id, news_id, lookback_hours)。外键：(tenant_id, snapshot_id) → reader_snapshot。')
fields('news_audience.news_reader_topic',[
 ('tenant_id','uuid','否','业务租户 ID'),
 ('snapshot_id','text','否','与人群行一致的快照 ID'),
 ('news_id','text','否','新闻 ID'),
 ('lookback_hours','integer','否','与人群行一致的兴趣期限'),
 ('topic_id','text','否','兴趣主题 ID'),
 ('topic_reader_count','bigint','否','本篇读者中命中该兴趣的人数，≥ 0'),
 ('baseline_topic_count','bigint','否','基准人群中命中该兴趣的人数，≥ 本篇人数'),
 ('taxonomy_version','text','否','主题目录版本'),
 ('source_rank','integer','否','源主题排名，1 到 50；同组内唯一'),
], '主键：人群表四列主键 + topic_id。外键关联人群表、同目录快照和 topic_dictionary。')
para('主题可以重叠，标签人数不能相加得到 UV；未输出的主题不等于 0 人。导入校验要求标签人数不超过该期限的已画像人数。','small')

page('04  用户标签：窗口快照与标签目录', '每次读取人数和主题，都必须使用同一 tenant_id、snapshot_id 与 taxonomy_version。')
fields('news_audience.reader_snapshot',[
 ('tenant_id','uuid','否','业务租户 ID'),
 ('snapshot_id','text','否','受众快照 ID'),
 ('window_start','timestamptz','否','本次统计窗口起点'),
 ('window_end','timestamptz','否','窗口终点；窗口长度大于 0 且不超过 24h'),
 ('profile_cutoff','timestamptz','否','兴趣特征截止点，必须等于 window_start'),
 ('knowledge_as_of','timestamptz','否','数据知识截止时间，≥ window_end'),
 ('knowledge_as_of_basis','text','否','知识截止时间的来源说明'),
 ('history_start','timestamptz','否','兴趣历史起点，≤ profile_cutoff'),
 ('source_revision','bigint','否','源修订号，≥ 1'),
 ('source_release_sha256','text','否','源聚合包 manifest 原始字节的 SHA-256'),
 ('source_mode','text','否','synthetic / enterprise'),
 ('taxonomy_version','text','否','主题目录版本'),
 ('profile_policy_version','text','否','兴趣计算规则版本'),
 ('identity_policy_version','text','否','读者身份及去重规则版本'),
 ('reader_definition','text','否','当前固定 detail-view-distinct-v1'),
 ('count_method','text','否','exact_synthetic_distinct / enterprise_exact_distinct'),
 ('generated_at','timestamptz','否','快照生成时间，≥ knowledge_as_of'),
 ('watermark_at','timestamptz','否','已处理数据水位，≤ knowledge_as_of'),
 ('completeness','text','否','incomplete / complete / corrected'),
 ('quality_status','text','否','pending / passed / failed'),
 ('quality_basis','text','否','质量校验依据'),
 ('expires_at','timestamptz','是','过期时间；enterprise 来源必须提供'),
 ('min_support','bigint','否','lift 最小样本门槛，默认 5，且 ≥ 5'),
], '主键：(tenant_id, snapshot_id)。唯一：(tenant_id, source_release_sha256, window_start, window_end)。')
fields('news_audience.topic_dictionary',[
 ('taxonomy_version','text','否','主题目录版本'),
 ('topic_id','text','否','主题 ID'),
 ('topic_name','text','否','主题显示名称'),
 ('parent_topic_id','text','是','父主题 ID；一级主题必须为空'),
 ('topic_level','smallint','否','一级为 1；下级大于 1 且必须有父主题'),
], '主键：(taxonomy_version, topic_id)。父主题外键包含同一 taxonomy_version。')
para('快照外键关联 package_release。complete / corrected 要求 watermark_at ≥ window_end。SQL 查询仍需显式过滤 quality_status、completeness 与 expires_at。','small')

page('05  实时新闻与用户标签同屏查询', 'news_realtime.news_reader_overview 是当前已定义的查询视图；每行 = 业务租户 + 一分钟窗口 + 新闻。')
overview=[
 ('窗口_北京时间','timestamp','新闻统计分钟起点（北京时间）'),
 ('新闻标题','text','标题，可空'),
 ('新闻内容标签','text','主题名以 / 分隔；无标签可为空'),
 ('详情读者人数','bigint','详情去重读者；尚无画像时为空'),
 ('近24小时兴趣_TOP3','text','短期兴趣前三项，含占全部读者百分比'),
 ('近30天兴趣_TOP3','text','长期 720 小时兴趣前三项'),
 ('窗口状态','text','暂定 / 已定稿'),
 ('同步检查','text','检查有效 / 检查已过期'),
 ('曝光次数','bigint','本分钟曝光次数'),
 ('点击次数','bigint','本分钟点击次数'),
 ('指标UV','bigint','本分钟点击去重人数'),
 ('短期已画像人数','bigint','24h 有已知兴趣的读者数；无画像为 NULL'),
 ('短期未知人数','bigint','24h 未知兴趣读者数；无画像为 NULL'),
 ('短期冷启动人数','bigint','24h 冷启动读者数；无画像为 NULL'),
 ('长期已画像人数','bigint','720h 有已知兴趣的读者数；无画像为 NULL'),
 ('长期未知人数','bigint','720h 未知兴趣读者数；无画像为 NULL'),
 ('长期冷启动人数','bigint','720h 冷启动读者数；无画像为 NULL'),
 ('最新事件_北京时间','timestamp','最新已处理事件的发生时间'),
 ('入库时间_北京时间','timestamp','投影进入 SQL 的时间'),
 ('检查时间_北京时间','timestamp','最近一次源状态检查时间'),
 ('数据来源','text','公开新闻缓存 + 模拟读者行为'),
 ('新闻链接','text','原始 URL，可空'),
 ('新闻正文','text','全文，可空'),
 ('完整人群画像_JSON','jsonb','已输出的完整聚合画像；尚无画像时为空'),
 ('tenant_id','text','业务租户 ID'),
 ('news_id','text','新闻 ID'),
 ('window_key','text','分钟窗口唯一身份'),
 ('window_start','timestamptz','统计窗口起点'),
 ('window_end','timestamptz','统计窗口终点 = 起点 + 1 分钟'),
 ('revision','bigint','当前窗口修订号'),
 ('catalog_id','text','本次内容目录 ID'),
 ('content_release_id','text','正文发布 ID'),
 ('tag_release_id','text','标签发布 ID'),
]
documented_fields['news_realtime.news_reader_overview']=[r[0] for r in overview]
table(['字段名（可直接 SELECT）','SQL 类型','含义'],overview,[166,85,W-251])
para('TOP3 按标签读者数降序、topic_id 打破平局。百分比 = 标签读者数 / 全部详情读者数；兴趣可以重叠。检查有效不代表窗口正好是当前分钟。','small')
para('完整人群画像_JSON：reader_count 为总人数；short_term / long_term 含 lookback_hours、profiled_reader_count、unknown_reader_count、cold_start_reader_count、baseline_profiled_count、topics。topics 内含 topic_id、reader_count、share_all、share_profiled、baseline_reader_count、baseline_profiled_count、baseline_share、lift、low_sample。','small')

page('06  直接查询与字段计算', '下面 SQL 查询已有对象。第 1 段读取最近已入库窗口；第 2 段查询已导入的固定示例快照，不与实时分钟结果拼接。')
sub('1. 查新闻、指标和用户兴趣（统一新闻数据库）')
code('''SELECT "窗口_北京时间", "新闻标题", "新闻内容标签",
       "曝光次数", "点击次数", "指标UV", "详情读者人数",
       "近24小时兴趣_TOP3", "近30天兴趣_TOP3",
       "窗口状态", "同步检查", "完整人群画像_JSON", news_id
FROM news_realtime.news_reader_overview
WHERE tenant_id = '11111111-1111-4111-8111-111111111111'
  AND window_start = (
    SELECT max(window_start) FROM news_realtime.news_live_current
    WHERE tenant_id = '11111111-1111-4111-8111-111111111111'
  )
ORDER BY "详情读者人数" DESC NULLS LAST, news_id
LIMIT 20;''')
sub('2. 查一篇新闻的短期、长期兴趣及分母（同库 news_audience）')
code('''SELECT news_id, lookback_hours, topic_id, topic_name,
       topic_reader_count, reader_count, profiled_reader_count,
       share_all, share_profiled, baseline_share, lift, low_sample
FROM news_audience.v_news_reader_topic_metrics
WHERE tenant_id = '11111111-1111-4111-8111-111111111111'::uuid
  AND source_release_sha256 =
      '03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5'
  AND snapshot_id =
      '90d919aee5b20f16d3bc3200e91f65d55120b69458388ba6c883a96d1db4954d'
  AND news_id = '20260718A03B9V00'
  AND completeness IN ('complete', 'corrected')
  AND quality_status = 'passed' AND watermark_at >= window_end
  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
ORDER BY lookback_hours, topic_reader_count DESC, topic_id;''')
sub('已实现的计算字段（news_audience 视图）')
table(['字段','计算规则'],[
 ('profile_coverage','已画像读者数 / 全部详情读者数'),
 ('share_all','标签读者数 / 全部详情读者数'),
 ('share_profiled','标签读者数 / 已画像读者数'),
 ('baseline_share','基准标签人数 / 基准已画像人数'),
 ('low_sample','本篇与基准的已画像人数、标签人数，四项最小值 < min_support'),
 ('lift','share_profiled / baseline_share；低样本或基准占比为 0 时为 NULL'),
],[128,W-128])
para('上述 SQL 比率分母为 0 时返回 NULL。正文关联通过 unified_news_binding 显式映射业务租户与内容域，再同时匹配新闻 ID、正文发布 ID、record_sha256 和 body_sha256。','small')
para('来源定义：app/sql_assistant/warehouse.py；app/modules/news_storage/{store.py,tag_store.py,realtime_002.sql,realtime_004.sql,realtime_005.sql}；app/modules/reader_profiles/warehouse.sql。路径均相对 agent/。','small')

def decorate(canvas, doc):
    canvas.setTitle('NewsAgent 当前新闻与用户标签 - 字段手册')
    canvas.setAuthor('NewsAgent')
    canvas.setStrokeColor(TEAL)
    canvas.setLineWidth(1)
    canvas.line(32,A4[1]-24,A4[0]-32,A4[1]-24)
    canvas.setFont('CN',7)
    canvas.setFillColor(GRAY)
    canvas.drawString(32,20,'NewsAgent | 当前字段字典 | 2026-10-25')
    canvas.drawRightString(A4[0]-32,20,str(doc.page))

SimpleDocTemplate(str(OUT),pagesize=A4,leftMargin=32,rightMargin=32,
                  topMargin=34,bottomMargin=34).build(story,onFirstPage=decorate,onLaterPages=decorate)
reader=PdfReader(str(OUT))
texts=[pg.extract_text() for pg in reader.pages]
assert len(texts)==len(page_titles), (len(texts),len(page_titles))
for text,title in zip(texts,page_titles):
    assert ''.join(title.split()) in ''.join(text.split()), title
for banned in ['na_contract','news_sim_v8','企业映射建议','阅读路线','待实现 / 待确认']:
    assert banned not in '\n'.join(texts), banned
report={'pages':len(texts),'page_titles':page_titles,'documented_fields':documented_fields,
        'sha256':hashlib.sha256(OUT.read_bytes()).hexdigest(),
        'schema_changes':False,'live_database_executed':False}
(QA/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
(QA/'extracted.txt').write_text('\n\n'.join(texts),encoding='utf-8')
print(json.dumps({'output':str(OUT),'pages':len(texts),'sha256':report['sha256']},ensure_ascii=False))
