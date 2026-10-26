"""Render the reviewed NewsAgent/TDSQL integration reference; never connects to a DB.

Run with a Python environment containing reportlab and pypdf. Font paths can be
overridden with PDF_CJK_REGULAR and PDF_CJK_BOLD; no .env files are read.
"""
from pathlib import Path
from xml.sax.saxutils import escape
import hashlib
import json
import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Preformatted
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'output/pdf/NewsAgent_热点新闻与用户标签_TDSQL接入表结构_2026-10-25.pdf'
QA = ROOT / 'tmp/pdfs/tdsql-schema-20261025'
REGULAR = os.environ.get('PDF_CJK_REGULAR', 'C:/Windows/Fonts/msyh.ttc')
BOLD = os.environ.get('PDF_CJK_BOLD', 'C:/Windows/Fonts/msyhbd.ttc')
pdfmetrics.registerFont(TTFont('CN', REGULAR, subfontIndex=0))
pdfmetrics.registerFont(TTFont('CNB', BOLD, subfontIndex=0))
pdfmetrics.registerFontFamily('CN', normal='CN', bold='CNB')
INK = colors.HexColor('#173047')
TEAL = colors.HexColor('#007F82')
MUTED = colors.HexColor('#596D7C')
PALE = colors.HexColor('#EDF5F7')
LINE = colors.HexColor('#D8E3EA')
W = A4[0] - 76
styles = {
    'title': ParagraphStyle('title', fontName='CNB', fontSize=22, leading=31, textColor=INK, spaceAfter=12),
    'deck': ParagraphStyle('deck', fontName='CN', fontSize=11, leading=18, textColor=MUTED, spaceAfter=13),
    'body': ParagraphStyle('body', fontName='CN', fontSize=9.4, leading=15, textColor=INK, spaceAfter=8, wordWrap='CJK'),
    'small': ParagraphStyle('small', fontName='CN', fontSize=8.1, leading=12.3, textColor=MUTED, spaceAfter=7, wordWrap='CJK'),
    'cell': ParagraphStyle('cell', fontName='CN', fontSize=8.25, leading=12.1, textColor=INK, wordWrap='CJK'),
    'th': ParagraphStyle('th', fontName='CNB', fontSize=8.5, leading=12.5, textColor=colors.white, wordWrap='CJK'),
    'sub': ParagraphStyle('sub', fontName='CNB', fontSize=11.5, leading=17, textColor=TEAL, spaceBefore=9, spaceAfter=7),
    'code': ParagraphStyle('code', fontName='Courier', fontSize=8.2, leading=11.8, textColor=INK, borderPadding=10, backColor=PALE, spaceAfter=9),
}
story = []
page_titles = []

def p(text, style='body'):
    return Paragraph(escape(text).replace('\n', '<br/>'), styles[style])

def para(text, style='body'):
    story.append(p(text, style))

def sub(text):
    para(text, 'sub')

def table(headers, rows, widths=None):
    widths = widths or [W/len(headers)]*len(headers)
    data = [[p(x, 'th') for x in headers]] + [[p(str(x), 'cell') for x in row] for row in rows]
    t = Table(data, colWidths=widths, repeatRows=1, hAlign='LEFT')
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), INK),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F5F8FA')]),
        ('LINEBELOW', (0, 0), (-1, 0), .6, TEAL),
        ('LINEBELOW', (0, 1), (-1, -1), .35, LINE),
    ]))
    story.append(t)
    story.append(Spacer(1, 9))

def page(number, title, summary):
    if story:
        story.append(PageBreak())
    page_titles.append(title)
    para(f'{number:02d} / NEWSAGENT DATA CONTRACT', 'small')
    para(title, 'title')
    para(summary, 'deck')

def note(text):
    t = Table([[p(text)]], colWidths=[W])
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),PALE),('BOX',(0,0),(-1,-1),.5,LINE),
                           ('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10),
                           ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
    story.append(t)
    story.append(Spacer(1,8))

def code(text):
    assert max(map(len, text.splitlines())) <= 96, 'SQL line too wide'
    story.append(Preformatted(text.strip(), styles['code']))


page(1, '热点新闻与用户标签\nTDSQL 接入表结构', 'NewsAgent 技术交接参考 | v1.0 | 2026-10-25（北京时间）')
note('目标：让腾讯新闻内部数仓团队能够按字段、粒度、SQL 与验收条件映射现有数据，支撑“看热点 - 看读者兴趣 - 找关联证据 - 生成人工研判”。本文不要求重建内部数仓。')
para('企业背景由用户确认：内部使用自研实时数仓，TDSQL 体系，以 SQL 查询；此前项目上下文已确认 TDSQL-A / PostgreSQL 兼容方向。内部实例的版本、授权方式、物理表名与指标口径仍需由内部团队提供。')
table(['文档标记', '含义'], [
    ('现有实现', '已对照当前工作区源码、DDL 和配置核实；本次未连接数据库验证部署状态。'),
    ('建议合同', '建议向企业开放的受控 SQL 视图、字段和适配规则；名称为逻辑示例，不是腾讯内部真实表名。'),
    ('待确认 / 待实现', '需要内部表字典、权限、口径或新增适配代码；不能作为已接通企业的证明。'),
], [100, W-100])
sub('阅读路线')
table(['页码', '内容', '适用人员'], [
    ('02-07', '当前表结构、新闻标签、读者聚合及统计口径', '应用研发 / 数据分析'),
    ('08-10', '企业 SQL 视图合同、快照元数据、用户画像边界', '数仓 / 画像 / 数据平台'),
    ('11-12', '参数化 SQL 查询示例与一致性处理', 'SQL 接入研发'),
    ('13-14', 'Python Port 接线、正文与分层向量召回', 'Agent / 检索研发'),
    ('15-16', '配置、验收、待办与来源', '联调负责人 / 评审'),
], [55, 295, W-350])
para('主线：权威数字由 SQL / Python 计算，模型负责解释。企业原始访问明细和逐用户兴趣留在企业侧，NewsAgent 只消费必要的聚合、版本与证据引用。正文内容、检索文本和模型输出均按不可信数据处理。')
para('公开资料只用于核对技术方向：腾讯云 TDSQL-A 产品简介描述了 PostgreSQL 语法兼容及分析能力；这不能证明内部实例与公开产品完全相同。[E1] 本文 SQL 按 PostgreSQL 方言示意，不含生产建表或部署操作。', 'small')

page(2, '现有数据对象与职责', '同一个 news_id 串联内容、行为、读者兴趣与报告；租户、窗口和版本必须同时对齐。')
table(['数据域 / 对象', '一行代表什么', '用途与边界'], [
    ('dw.dim_news', '租户 + 新闻', '新闻属性；不是全文版本仓库。'),
    ('dw.news_metric_hourly', '租户 + 新闻 + 小时起点', '热点计算的窗口聚合。'),
    ('dw.news_metric_baseline_hourly', '租户 + 新闻 + 小时起点', '四项历史基线；缺行经 LEFT JOIN 输出 NULL。'),
    ('dw.news_behavior_aggregate', '上述关联后的同一小时新闻', '现有热点 SQL 工具的 18 列读取合同。'),
    ('news_sim_v8.*', '独立扩展模拟数仓', '13 表覆盖渠道、5 分钟 / 小时 / 日、修订与生命周期；不等同 dw.*。'),
    ('public.news_content_*', '新闻版本 / 冻结发布 / 标签关系', '7 张正文与标签表；来源证据可追溯。'),
    ('news_audience.*', '冻结受众包 / 窗口 / 新闻 / 兴趣期限', '7 张聚合表；没有企业个人身份明细。'),
    ('news_realtime.*', '分钟快照、实时预览与最终读者聚合', '独立本地流式实验；公开正文 + 合成行为。'),
    ('analysis_runs', '一次租户热点窗口运行', '幂等身份、Bundle、处理数量、榜单和分析快照。'),
    ('hot_events / hot_news_decisions', '聚合事件 / 运营决策', '应用状态；不是数仓行为事实。'),
], [173, 141, W-314])
sub('正文、标签、聚合的关联顺序')
para('正文 release 选定 news_id 的内容版本 → 内容标签 release 绑定同一正文版本 → unified_news_binding 按 news_id + 非空 body_sha256 连接业务受众 → reader_snapshot 确定窗口 → population / topic 读取人群与主题。')
note('内容域 public-news-local 使用文本身份，业务受众租户使用 UUID。二者不能直接按 tenant_id 等值连接。企业接入若使用业务线 / app_id，应由可信身份配置完成显式映射。')
para('热点来源：app/sql_assistant/warehouse.py；应用结果：modules/hot_news/persistence/hot_news.py。统一库及实时表的定义见 [R2-R5]。以上为代码结构核对，不复述旧文档行数为当前数据库实测。', 'small')

page(3, '热点 SQL 合同：18 个核心字段', '现有实现 | dw.news_behavior_aggregate；唯一粒度为租户、新闻、一个完整小时。')
table(['字段', '类型 / 空值', '定义与企业映射要求'], [
    ('tenant_id', 'uuid / 必填', '可信业务隔离域；所有筛选、关联、缓存均带此字段。'),
    ('news_id', 'text / 必填', '稳定新闻 ID；与正文、标签及索引采用同一身份。'),
    ('event_time', 'timestamptz / 必填', '小时起点；区间为 [start, end)，不是事件入库时间。'),
    ('title', 'text / 必填', '新闻标题；来自批准的内容维度。'),
    ('content_type', 'text / 必填', 'article / video；企业实际类型需映射。'),
    ('category', 'text / 必填', '新闻栏目；不是用户兴趣，也不是访问渠道。'),
    ('source', 'text / 必填', '内容来源展示标记；不是推荐、搜索等流量入口。'),
    ('publish_time', 'timestamptz / 必填', '真实发布时间；不得使用接入时间代替。'),
    ('impressions', 'bigint / 必填', '有效曝光事件次数；去重、可见性口径由企业定义。'),
    ('clicks', 'bigint / 必填', '点击事件次数；归因规则与曝光计数须对齐。'),
    ('unique_users', 'bigint / 必填', '目标新闻、目标小时的点击 UV；不是详情读者数。'),
    ('total_duration_seconds', 'numeric(20,2) / 必填', '累计消费秒数；现有领域对象要求整数秒，见第 13 页。'),
    ('effective_consumptions', 'bigint / 必填', '有效阅读 / 播放次数；当前模拟为点击归因口径。'),
    ('interactions', 'bigint / 必填', '批准的互动事件总次数，不是互动 UV。'),
    ('baseline_impressions', 'numeric(20,2) / 可空', '曝光历史基线；无样本输出 NULL。'),
    ('baseline_clicks', 'numeric(20,2) / 可空', '同口径点击基线；零基线与无基线区分。'),
    ('baseline_effective_consumptions', 'numeric(20,2) / 可空', '有效消费基线；不得引入目标窗口后的样本。'),
    ('baseline_interactions', 'numeric(20,2) / 可空', '互动基线；使用同单位和口径。'),
], [177, 119, W-296])
para('物理主键：dim_news 为 (tenant_id, news_id)；指标、基线为 (tenant_id, news_id, event_time)。基线物理列非空，但无基线可以不插入该行，视图 LEFT JOIN 后为空。[R1]', 'small')
para('热点默认分数 = 0.30×点击率 + 0.35×有效消费率 + 0.20×互动分量 + 0.15×压缩增长分量；比例被限定在 [0,1]，增长仅奖励正增长。实际运行须绑定批准规则版本；这不是企业已有热榜的原始分数。[R6]', 'small')

page(4, '统一正文与新闻内容标签', '现有实现 | public Schema 的 7 张表；标签描述新闻内容，不描述读者。')
table(['对象 / 主键', '主要字段与用途'], [
    ('news_content_versions\nPK: tenant_id, news_id, record_sha256', 'title、body、url、publish_time、content_type、content_state、body_sha256、record(JSONB)。record_sha256 是整条标准化记录摘要；body_sha256 仅为正文摘要。'),
    ('news_content_releases\nPK: tenant_id, release_id', 'identity_sha256、schema_version、selection_policy、status、manifest(JSONB)、created_at。冻结选择结果，不隐式取 latest。'),
    ('news_content_release_items\nPK: tenant_id, release_id, news_id', 'record_sha256、quality_flags(JSONB)。每个 release 为每篇新闻选定一个内容版本。'),
    ('news_content_provenance\nPK: tenant_id, release_id, news_id, record_sha256, source_sha256', 'source(JSONB) 保存来源包、路径、原文件哈希、采集时点和选择优先级等证据。'),
    ('news_content_tag_releases\nPK: tenant_id, content_release_id, tag_release_id', 'content_identity_sha256、identity_sha256、schema_version、derivation_version、policy_version、taxonomy_version、tagger_sha256、status、manifest、policy、created_at。'),
    ('news_content_tags\nPK: tenant_id, content_release_id, tag_release_id, news_id', 'record_sha256、body_sha256、label_sha256、label_status、input_scope、provenance、topic_ids(JSONB)、label(JSONB)。label 保留规则命中及证据位置。'),
    ('news_content_topic_memberships\nPK: 上述标签主键 + topic_id', '将一篇新闻的多个主题拆成关系行；主题过滤发生在分页之前，包含继承父主题。'),
], [224, W-224])
sub('企业映射')
para('建议企业提供稳定 news_id、内容版本 / 哈希、正式栏目、状态和正文对象引用；正文仍由内容服务或对象存储读取。NewsAgent 本地公开正文目前在 PostgreSQL，不能据此要求内部把全文搬进实时数仓。')
note('label_status：tagged / unclassified / insufficient_input / metadata_review。现有 12 个一级主题、72 个二级主题为本地草案；生产须绑定企业批准目录。规则推断标签、企业栏目、人工金标必须分别标识。')
para('查询视图：news_content_selected 与 news_content_tagged_selected。类型细节与完整说明见 [R2]；实现见 modules/news_storage/store.py、tag_store.py。', 'small')

page(5, '受众表：来源、绑定与窗口', '现有实现 | news_audience；这一页固定“这批人群数字来自哪里、对应哪篇正文”。')
table(['对象 / 粒度', '字段组与约束'], [
    ('package_release\nPK: source_release_sha256', 'manifest、policy、schema_version、row_counts、projection_digests、imported_at。来源身份为 manifest 原始字节 SHA-256，导入后按摘要核对投影。'),
    ('content_binding\nPK: tenant_id, source_release_sha256, news_id', 'content_sha256（可空）、content_topic_ids、binding。表示源包有这篇新闻；不能据此认定统一正文已匹配。'),
    ('unified_news_binding\nPK: 上述三字段 + content_tenant_id, content_release_id', 'record_sha256、body_sha256、binding_method、bound_at。按 news_id + 非空正文哈希精确映射，禁止按相似标题猜测。'),
    ('reader_snapshot\nPK: tenant_id, snapshot_id', 'window_start/end、profile_cutoff、knowledge_as_of、knowledge_as_of_basis、history_start；窗口与特征截止时点分开。'),
    ('reader_snapshot / 来源与版本', 'source_revision、source_release_sha256、source_mode、taxonomy_version、profile_policy_version、identity_policy_version、reader_definition、count_method。'),
    ('reader_snapshot / 质量与时效', 'generated_at、watermark_at、completeness、quality_status、quality_basis、expires_at、min_support。唯一约束另包含 tenant + release + window。'),
    ('topic_dictionary\nPK: taxonomy_version, topic_id', 'topic_name、parent_topic_id、topic_level。父主题外键包含同一 taxonomy_version。'),
], [210, W-210])
sub('固定范围与时间语义')
para('当前冻结受众导入表将一个来源 release 的同一租户、同一窗口限定为一份覆盖快照。它不能直接承担无限实时修订追加；企业实时接口建议采用第 8-9 页的独立快照合同，而不是原地修改冻结表的含义。')
para('profile_cutoff = window_start，历史特征不得越过它。knowledge_as_of 是本地包冻结时点；不能冒充企业事件实际到达的历史时点。源内容绑定和窗口覆盖共同成立，才能解释缺失人群行。')
note('企业读者人数全部由上游在批准身份域内去重。NewsAgent 不接收 user_id、设备 Token 或原始浏览日志。个人行为目前仅存在隔离的合成实验中。[R3]')

page(6, '受众表：人数与兴趣主题', '现有实现 | 人数表存分母，主题表存分子；关联必须包括租户、快照、新闻和兴趣期限。')
table(['news_reader_population 字段', '类型', '说明'], [
    ('tenant_id / snapshot_id / news_id', 'uuid / text / text', '新闻窗口身份；构成复合主键前三项。'),
    ('lookback_hours', 'integer', '主键第四项；当前表只允许 24 / 720。'),
    ('content_sha256 / source_has_body_hash', 'text? / boolean', '正文绑定；保留源哈希是否确实存在。'),
    ('reader_count', 'bigint', '该窗口进入新闻详情页的去重读者数。'),
    ('profiled_reader_count', 'bigint', '该兴趣期限内具有可用画像的人数。'),
    ('unknown_reader_count / cold_start_reader_count', 'bigint / bigint', '未知 = 总读者 - 有画像；冷启动是未知的子集。'),
    ('baseline_profiled_count', 'bigint', '同口径基准人群中有画像人数。'),
    ('status / trigger_article_excluded', 'text / boolean', 'complete 或 empty；排除触发新闻自身历史贡献。'),
    ('limitations / source_payload_sha256', 'text[] / text', '历史不足等限制；原聚合载荷摘要。'),
    ('source_topic_limit / topics_are_top_k / source_topic_count', 'integer / boolean / integer', '源 Top-K 上限、是否截断型输出、实际主题条数。'),
], [245, 100, W-345])
table(['news_reader_topic 字段', '类型', '说明'], [
    ('tenant_id / snapshot_id / news_id / lookback_hours / topic_id', '复合主键', '一篇新闻、一个兴趣期限、一个保留主题。'),
    ('topic_reader_count', 'bigint', '读者中命中该主题的人数。'),
    ('baseline_topic_count', 'bigint', '基准人群中命中该主题的人数。'),
    ('taxonomy_version', 'text', '连接版本化字典与快照。'),
    ('source_rank', 'integer', '保留上游主题排名；同一人群内唯一。'),
], [245, 100, W-345])
para('索引覆盖按新闻查快照、按主题人数排序、按主题筛新闻。SQL 视图 v_news_reader_population_status 返回画像覆盖率；v_news_reader_topic_metrics 返回占比、基准、lift 和低样本标记。[R3]', 'small')
para('当前精确计数合同要求主题人数不超过画像分母，读者群包含于其基准群；这些跨表条件部分由导入和 Python 校验承担，不应误称全部已由数据库 CHECK 保证。', 'small')

page(7, '用户标签与统计口径', '同一新闻可以有多个内容主题，同一个读者也可以属于多个兴趣主题。')
table(['指标 / 规则', '确定定义', '接入约束'], [
    ('详情读者 reader_count', '在 [window_start, window_end) 内有 detail_view 的 distinct 身份数', '点击 UV、曝光 UV、详情 PV 都不能替代它。'),
    ('短期 / 长期兴趣', '[start-24h, start) / [start-720h, start)', '分别计算；不是把小时读者数或每日画像直接求和。'),
    ('访问前画像', 'profile_cutoff 不晚于目标窗口起点', '窗口内新行为不能倒灌解释本次受众。'),
    ('触发新闻排除', '移除目标 news_id 的全部历史贡献', '包括历史点击、阅读、互动和负反馈。现成用户标签未必支持。'),
    ('share_all', 'topic_reader_count / reader_count', '占全部详情读者比例。'),
    ('share_profiled', 'topic_reader_count / profiled_reader_count', '占有画像读者比例；分母可能小于总读者。'),
    ('baseline_share', 'baseline_topic_count / baseline_profiled_count', '基准范围、同样的期限及目标新闻排除规则必须明确。'),
    ('lift', 'share_profiled / baseline_share', '低样本或基准为零时不输出；不是因果效果。'),
    ('低样本', '现有策略对两个人群分母和两个主题分子取最小值，低于 5 则抑制 lift', '企业阈值改变必须另设策略版本。'),
    ('Top-K / 父子主题', '主题可重叠；未返回的主题不代表零', '不能把主题人数求和得到总人数。'),
], [120, 215, W-335])
sub('缺失、零值与精度')
para('仅当新闻在来源范围内，目标窗口完整且有明确覆盖，才能将“没有详情访客”输出为 empty / 0。未绑定新闻、未覆盖窗口、失败或过期应返回 unavailable / 错误，不能补零。')
para('SQL 用 NULLIF 保留零分母为 NULL；现有 Reader DTO 的比例字段使用八位 HALF_EVEN 且零分母返回 0。Adapter 须明确执行该既有边界转换并保留 empty 状态，不能把 SQL NULL 误当原始计数缺失。')
note('当前 Reader DTO 允许带 error_bound 的近似人数，但 news_audience 冻结 SQL 表仅接受 exact 计数，严格精度校验也未证明兼容任意近似算法。建议首版企业接入用精确聚合；近似计数另立版本并验收。[R3-R4]')

page(8, '建议开放的企业 SQL 视图', '建议合同 | na_contract 是本文示例 Schema。企业可以用已有表的受控视图或 SQL 网关提供相同语义。')
table(['建议视图 / 唯一粒度', '返回内容', '对应现有能力'], [
    ('snapshot_v1\ntenant + domain + snapshot_id', '窗口、修订、水位、完整性、质量、来源与版本；domain = metrics / readers', '补齐现有 18 列视图缺失的快照元数据。'),
    ('news_metric_v1\ntenant + snapshot_id + news_id', '第 3 页 18 列 + content_version、content_sha256、baseline_version、baseline_sample_count', '映射 NewsMetricSnapshot / 基线；当前指标视图无 snapshot_id。'),
    ('news_content_ref_v1\ntenant + news_id + content_version', 'title、content_type、category、publish_time、content_sha256、content_status、lifecycle_revision、status_effective_at、body_ref、source_url?', '映射 NewsContentRepository；正文通过 body_ref 解析。'),
    ('news_content_tag_status_v1\ntenant + news_id + content_version + tag_version', 'taxonomy_version、tag_status、label_origin、content_sha256', '即使没有主题，也明确记录未分类或输入不足。'),
    ('news_content_topic_v1\n标签状态键 + topic_id', 'taxonomy_version、confidence?、evidence_ref?', '映射内容主题；外键指向同版本标签状态。'),
    ('topic_dictionary_v1\ntenant + taxonomy_version + topic_id', 'topic_name、parent_topic_id?、topic_level、status', '企业批准 TopicCatalog；需显式授权共享目录。'),
    ('news_reader_population_v1\ntenant + snapshot_id + news_id + lookback_hours', '第 6 页人口字段；来源版本通过 snapshot 关联', 'ReaderProfileSummary 的人数和长期 / 短期分母。'),
    ('news_reader_topic_v1\n人口键 + topic_id', 'topic_reader_count、baseline_topic_count、taxonomy_version、source_rank', 'ReaderTopic；分子和分母分开返回。'),
], [222, 193, W-415])
para('类型约定：外部身份和版本使用受限字符串；计数为 bigint；比例采用 decimal；时间使用带时区时间戳；哈希为 64 位十六进制字符串。tenant 到当前 UUID 的映射只由服务身份配置完成。问号表示可空。逻辑视图不强制企业采用 JSONB、触发器或同样物理表。')
sub('发布协议')
para('一次发布先完整生成同一 snapshot_id 下的数据，再原子发布快照清单。清单的“已发布”状态与业务质量状态分开；最新已发布修订即使质量不合格也不能悄悄回退旧版。每个 snapshot 的事实行不可原地修改。')
para('指标与画像可有不同 snapshot_id，运行记录二者；必须同租户、同窗口、内容绑定一致，并各自通过完整性门禁。跨系统若无一致快照令牌，需要冻结结果与版本引用后再分析。', 'small')

page(9, '建议合同：快照与版本元数据', '建议合同 | 在现有核心业务字段之外补齐；不能只增加一个 revision 就声称获得一致快照。')
table(['snapshot_v1 字段组', '类型 / 必填条件', '语义'], [
    ('tenant_id / domain / snapshot_id', 'text / 必填', '逻辑主键；domain 限 metrics / readers。'),
    ('window_start / window_end', 'timestamptz / 必填', '准确目标区间；同一窗口各修订为替代关系。'),
    ('source_revision / schema_version', 'bigint / text / 必填', '窗口内单调修订；结构合同版本。'),
    ('publication_status / completeness', 'text / 必填', '发布标志 published；完整性 incomplete / complete / corrected。'),
    ('quality_status / quality_basis', 'text / 必填', 'passed / pending / failed 及判定规则版本或引用。'),
    ('watermark_at / generated_at', 'timestamptz / 必填', '处理水位与结果生成时间；完整窗口至少 watermark ≥ end。'),
    ('expires_at', 'timestamptz / 企业在线必填', '结果时效；过期不冒充当前可用。'),
    ('source_release_sha256 / data_origin', 'text / 必填', '不可变来源发布身份；enterprise_measured / synthetic 等枚举。'),
    ('metric_definition_version / uv_method', 'text / metrics 必填', '指标、身份去重方法版本；不以本地模拟算法代替企业口径。'),
    ('taxonomy_version / profile_policy_version', 'text / readers 必填', '批准的目录与兴趣策略。'),
    ('identity_policy_version / count_method', 'text / readers 必填', '身份合并规则；首版 enterprise_exact_distinct。'),
    ('profile_cutoff / history_start', 'timestamptz / readers 必填', '特征可见边界与实际可用历史；不足 720h 必须标记。'),
    ('knowledge_as_of / evidence_ref', '时间 / 引用 / 必填', '实际数据可见时点和受控快照证据；不能以本地时间猜测。'),
    ('content_snapshot_ref / expected_row_count', 'text / bigint / 必填', '关联内容版本集；校验域内主事实行数。readers 指人口行数。'),
    ('result_digest', 'text / 建议必填', '按约定顺序与规范化规则计算的结果摘要。'),
], [236, 128, W-364])
para('查询回执另保存 query_id、elapsed_ms、truncated、实际返回行数和快照引用；query_id 是执行追踪号，不是快照或业务幂等键。网关没有原生快照令牌时，应证明一次受控一致性读取覆盖候选、补充指标和基线。')
note('实时来源 release 轮换是接入缺口：当前 Reader 工厂静态绑定一个 SHA。生产滚动发布需要明确不可变 release 的覆盖范围及受控切换机制；不能随意用查询时间或 snapshot_id 代替来源发布身份。')

page(10, '企业侧用户标签如何形成聚合', '建议合同 | 可以映射企业已有画像平台；不要求 NewsAgent 自建推荐算法或拉取个人行为。')
table(['仅在企业侧的逻辑数据', '建议关键字段', '使用约束'], [
    ('用户画像覆盖快照\n一用户、一特征截止、一策略', 'tenant_id、user_key、profile_cutoff、profile_version、known、cold_start、history_start、generated_at', 'user_key 由企业身份系统管理；未命中主题不等于没有画像。'),
    ('用户 - 兴趣主题关系\n覆盖快照 + 期限 + topic', 'user_key、lookback_hours、taxonomy_version、topic_id、interest_score、member_flag、profile_policy_version', '保留得分与成员阈值含义；标签多值；不能直接把概率当人数。'),
    ('新闻详情访客集合\n租户、新闻、目标窗口、用户', 'tenant_id、news_id、window_start/end、user_key、identity_policy_version、content_version', '企业内做去重集合；不传出个人列表。'),
    ('按目标新闻校正的画像 / 聚合', 'target_news_id、profile_cutoff、lookback_hours、trigger_article_excluded、policy_version', '必须移除目标新闻历史贡献；仅有一份通用用户标签快照通常做不到。'),
], [180, 194, W-374])
sub('企业内计算顺序')
para('1. 固定租户、新闻、窗口、内容版本和画像截止时点；对详情访客做 distinct。\n2. 获取截止时点前的长短期画像，并对目标新闻执行贡献排除。\n3. 按同一身份、期限、策略生成可比基准群；该群须包含目标读者群。\n4. 计算读者分母、主题人数、未知 / 冷启动、基准分母及基准主题人数。\n5. 先完成全量主题统计再按约定 Top-K 截断，保留源排名与限制。\n6. 原子发布人口表、主题表和快照清单；NewsAgent 只查询聚合视图。')
note('若内部已有标签不能排除目标新闻：应明确“无法满足当前策略”，新增经审核的策略与解释方式后再接入，不能把 trigger_article_excluded 填 true。企业实际兴趣算法也不必照搬本地 3 天半衰期和 0.2 阈值。')
para('可选推送受众预估属于另一条聚合合同：主题交并集、排除主题、通知资格、频控和去重覆盖由企业服务计算。读者兴趣表不含接收名单或设备 Token，也不能代替可推送人数；正式推送仍需审批与投递系统。[R7]', 'small')

page(11, 'SQL 示例：冻结快照后取热点', '建议模板 | 仅供受控 Adapter / 数仓视图开发；不是允许模型任意执行的 SQL。')
para('先在批准的一致性读取内查最新已发布修订，再由 Python 判完整性、质量和时效。下面不在选最新前过滤 passed，避免最新坏快照被旧好快照掩盖。命名参数交给驱动绑定。')
code('''SELECT snapshot_id, source_revision, completeness,
       quality_status, watermark_at, generated_at,
       expires_at, source_release_sha256,
       metric_definition_version, content_snapshot_ref
FROM na_contract.snapshot_v1
WHERE tenant_id = :tenant_id AND domain = 'metrics'
  AND window_start = :window_start
  AND window_end = :window_end
  AND publication_status = 'published'
ORDER BY source_revision DESC
LIMIT 1;''')
para('门禁通过后，固定返回的 :metric_snapshot_id。第一阶段沿用现有完整单小时；分钟或跨小时需要新合同，不能简单 SUM 小时 UV。')
code('''SELECT news_id, event_time, title, content_type,
       category, source, publish_time,
       impressions, clicks, unique_users,
       total_duration_seconds, effective_consumptions,
       interactions, baseline_impressions, baseline_clicks,
       baseline_effective_consumptions, baseline_interactions,
       content_version, content_sha256,
       baseline_version, baseline_sample_count
FROM na_contract.news_metric_v1
WHERE tenant_id = :tenant_id
  AND snapshot_id = :metric_snapshot_id
  AND event_time = :window_start
ORDER BY clicks DESC, news_id ASC
LIMIT :candidate_limit;''')
para('这里按点击取候选，最终热度由 Python 计算。即使随后计算热度，也只是该候选范围内的排名；要称“全域热度 TopN”，必须证明候选覆盖完整或另行批准候选策略。内容权限与有效状态在受控视图及正文读取时再次核验。')
table(['参数 / 回执', '约束'], [
    ('tenant_id / 窗口 / snapshot_id', '来自可信身份、获准业务窗口与服务端查回结果；不接受模型自行指定租户。'),
    ('candidate_limit / timeout / truncated', '设置有界候选、SQL deadline；意外截断或重复主键应失败，不伪装完整结果。'),
    ('结构与执行计划', '逐实例验证类型、参数、事务隔离、扫描范围；公共文档的兼容性不能代替内部验收。'),
], [170, W-170])

page(12, 'SQL 示例：读者兴趣聚合', '建议模板 | :reader_snapshot_id 按第 11 页同样方法从 readers 域选择并完成门禁。')
para('示例查单篇新闻的两个期限。返回粒度为一个期限的一个主题，人口分母会重复显示，不能在结果集上 SUM 人数。多篇使用驱动绑定的 ID 集合，每批不超过现有 Port 的 20 篇。')
code('''SELECT p.news_id, p.lookback_hours, p.reader_count,
       p.profiled_reader_count, p.unknown_reader_count,
       p.cold_start_reader_count, p.baseline_profiled_count,
       p.status, p.content_sha256, p.trigger_article_excluded,
       p.source_topic_limit, p.topics_are_top_k,
       p.source_topic_count, t.topic_id, d.topic_name,
       t.topic_reader_count, t.baseline_topic_count,
       t.source_rank,
       t.topic_reader_count::numeric
         / NULLIF(p.reader_count, 0) AS share_all,
       t.topic_reader_count::numeric
         / NULLIF(p.profiled_reader_count, 0) AS share_profiled,
       t.baseline_topic_count::numeric
         / NULLIF(p.baseline_profiled_count, 0) AS baseline_share
FROM na_contract.news_reader_population_v1 p
LEFT JOIN na_contract.news_reader_topic_v1 t
  ON t.tenant_id = p.tenant_id
 AND t.snapshot_id = p.snapshot_id AND t.news_id = p.news_id
 AND t.lookback_hours = p.lookback_hours
LEFT JOIN na_contract.topic_dictionary_v1 d
  ON d.tenant_id = t.tenant_id
 AND d.taxonomy_version = t.taxonomy_version
 AND d.topic_id = t.topic_id
WHERE p.tenant_id = :tenant_id
  AND p.snapshot_id = :reader_snapshot_id
  AND p.news_id = :news_id
  AND p.lookback_hours IN (24, 720)
ORDER BY p.lookback_hours, t.source_rank;''')
para('用 LEFT JOIN 保留零主题的人口行。Adapter 核对两个期限、主题唯一性、目录一致性、分子分母关系，并按第 7 页计算八位比例与 lift。不存在人口行时先核实新闻资格和覆盖；未经证明不能制造 empty。')
note('关联校验：读者快照与指标快照须同租户、同窗口；读者 content_sha256 与所用正文一致。正文或目录更新不得被缓存遮蔽。多次 SELECT 要共享可重读的快照 / 令牌；无法保证时拒绝形成完整分析。')
para('实时预览与最终快照分开：进行中窗口只能标 provisional。当前 ReaderProfileService 要求 is_complete，预览不能直接送入最终分析。窗口关闭还须满足迟到预算、源覆盖与质量条件。', 'small')

page(13, '接入现有 Python 调用链', '现有能力可复用；新增 SQL 适配层需要实现和验收。本次 PDF 不修改运行代码。')
table(['阶段', '现有入口 / 核心类', '企业对接工作'], [
    ('触发与规划', '/chat 或热点 API → 受限意图 → compile_query / Guard → Temporal', '服务端固定租户与批准视图；新合同另建版本，不改冻结 v1/v7。'),
    ('数仓执行', 'SqlWarehouseClient.execute(SqlQuery) → SqlQueryResult', '新增真实 TDSQL SQL 网关 / 协议 Adapter；只执行已校验模板。'),
    ('指标和热度', 'NewsMetricSnapshot → HotScoreCalculator → HotNewsRanker', '保留整数 / Decimal、单位、基线口径；不让 LLM 填权威数值。'),
    ('正文', 'NewsContentRepository → HotNewsEnrichmentService', '通过 news_id + 内容版本 / 哈希解析正文对象，校验权限和撤稿。'),
    ('读者画像', 'NewsReaderProfilePort → ReaderProfileService → ReaderProfileBatch', '建议新增 SqlNewsReaderProfileClient；现有工厂仅 local_package / enterprise_http。'),
    ('证据与解释', 'KnowledgeSearchClient → RelatedNewsReranker → NativeHotNewsAnalysisRunner → Validator', '只将通过校验的正文、关联证据和聚合画像送入模型。'),
    ('输出', 'analysis_runs / Artifact → API、SSE、运营页面', '保存双快照、版本、SQL 哈希、query_id 与受控证据引用。'),
], [75, 223, W-298])
sub('需要补齐的真实差异')
para('① 现有 SqlQueryResult 仅有 rows / elapsed_ms / truncated，没有快照或 query_id。建议在 Adapter 的运行回执中补充版本化审计结构，不把新字段强塞入严格业务 Schema。\n② 数据库时长支持小数秒，部分领域对象要求整数秒。必须批准精度策略后同步 Schema 和测试，不能隐式 int() 截断。\n③ 现有基线补充路径的 sample_count 可能只是模拟占位，企业必须返回真实样本数。\n④ Reader 静态 release、企业目录批准状态、画像策略和 Prompt 白名单须共同冻结；滚动版本解析尚需实现。')
note('资料说明了接口所需语义；真实企业 SQL 查询、权限、TDSQL 兼容性与负载验收仍未完成。内部已有 HTTP 聚合服务时可复用现有 enterprise_http；若要求直接 SQL，则实施上面的新 Adapter，不另起第二套热点业务流程。')
para('主要源码：clients/enterprise/sql_warehouse.py、modules/reader_profiles/contracts.py / service.py / factory.py、modules/hot_news/analysis/hot_news_enrichment.py。[R4、R6]', 'small')

page(14, '与分层向量召回的连接', '分层索引用于找证据；它不生成新闻热度，也不存企业个人兴趣向量。')
table(['对象', '关键字段 / 配置', '职责'], [
    ('tier_lifecycle_news', 'tenant_id、news_id、source_revision、generation、status、content_hash、desired_tier、active_tier、active_job_id、config', '正文和有效投影台账；决定查询可见性。'),
    ('tier_lifecycle_jobs', 'job_id、news_id、generation、target_tier、payload、records、chunk_ids、phase、status、cleanup_dirty', '可恢复任务与完整向量快照。'),
    ('Milvus 三集合\nhot / warm / cold', 'chunk_id、tenant_id、news_id、content_version、chunk_index、embedding_version、title、excerpt、source_url、publish_time_epoch、vector', '可重建的检索投影；每层集合独立，模型版本和维度一致。'),
    ('tier_lifecycle_vectors', 'chunk_id、tenant_id、tier、record', '仅本地持久 SQL 向量替身，不是企业 ANN 性能证明。'),
], [130, 254, W-384])
sub('入库与查询')
para('内容源 → 栏目准入 / 文本质量校验 → KnowledgeLifecycleService.accept → PG 任务 → Temporal Worker → split_text → EmbeddingPort → 写入并验证目标层 → 条件激活 → 清理旧层。\n查询 → Query Embedding → 三层并发 → GuardedTieredIndex 有效状态过滤 / 有界补搜 → 最终校验 → 新闻去重和加权排名融合 → RelatedNewsReranker → 证据白名单。[R8]')
table(['策略项', '当前实现 / 边界'], [
    ('分层与回热', '按新闻年龄和显式回热保持期；秒级本地阈值只用于演示。访问热度自动升层、不同量化精度主要在实验目录。'),
    ('更正 / 撤稿 / 迁移', '更正先挡旧版；撤稿先改查询状态再删除；迁移验证目标后切换，纯迁移可校验后复用向量。'),
    ('标签如何参与', '内容主题可用于筛选或批准的重排；可选本地合成兴趣加分有上限，不改变权威热度。读者聚合主要进入受众分析。'),
    ('当前接线区别', 'BGE 冻结快照为单 HOT 路由；实时实验为 SQL 词面召回。统一正文 release 尚未自动同步生命周期索引。'),
], [115, W-115])
para('健康栏目按明确来源 category 排除，不能按正文词语或推断 health 主题误删。企业正文在对象存储时，要建立内容版本、事件修订和台账映射；不能把任意字符串 content_version 直接强转生命周期的单调整数修订。', 'small')

page(15, '配置与企业验收清单', '以下是对接验收项目，不是本次已执行的测试结果；吞吐与延迟目标由内部约定。')
table(['验收项', '必须可重复验证的结果'], [
    ('字段映射与类型', '每个必需字段有源表 / 表达式 / 单位 / 空值 / 负责人；整型、Decimal、时区及新闻 ID 无损。'),
    ('窗口与修订', '迟到、补数、最新坏快照、读取中修订均可复现；不混版、不回退、不重复计数。'),
    ('UV 和读者', '点击 UV 与详情 UV 分开；跨小时目标 UV 独立重算；主题重叠不相加。'),
    ('画像因果边界', '窗口内未来行为与目标新闻历史贡献不影响当前画像；基准群与读者群使用同策略。'),
    ('空 / 缺失 / Top-K', '覆盖内零读者、未覆盖、失效、未知画像、少主题与截断分别正确表达。'),
    ('内容和权限', '租户隔离、内容哈希错配、撤稿、受限新闻、目录错版均被拦截。'),
    ('SQL 与资源', '参数绑定、白名单、只读角色、超时、取消、结果行数和字节数上限；异常不能伪装空结果。'),
    ('可追踪与回放', '每轮查询 / 快照 / Workflow / 模型调用可关联；汇总值、哈希和审计引用足以复现。'),
    ('规模和持续更新', '按实际分区、分布键、并发、修订频率压测 P50/P95/P99、扫描量和数据新鲜度；不外推本地样本。'),
], [112, W-112])
sub('配置入口与当前默认')
para('热点：HOT_NEWS_DEPENDENCIES_FACTORY；模型：MODEL_RUNTIME_CONFIG_PATH。\n画像：READER_PROFILE_ENABLED、BACKEND、TAXONOMY_PATH、SOURCE_RELEASE_SHA256、POLICY_VERSION、ALLOWED_ANALYSIS_PROMPT_VERSIONS；默认单次 3s、全轮 5s、缓存 30s、并发 16、响应 512 KiB。以上变量均带 READER_PROFILE_ 前缀。\n检索：KNOWLEDGE_SEARCH_CONFIG_PATH、KNOWLEDGE_LIFECYCLE_CONFIG_PATH。企业 SQL DSN / 网关地址、证书、角色与超时配置由新增 Adapter 定义，当前没有已完成的 TDSQL 配置开关。')
para('只读权限参考腾讯云官方说明 [E2]；实际内部角色、允许 Schema / 视图及租户访问控制以内部授权为准。建议按时间范围裁剪扫描，在企业内预聚合读者主题，评估分布键是否支持同域关联；不在未知内核上预写分布式 DDL。', 'small')
para('现有回归入口：test_sql_assistant_warehouse.py、test_reader_profile_contracts.py、test_reader_profile_client_service.py、test_reader_profile_warehouse.py、test_tier_lifecycle_runtime.py、test_tiered_vector_retrieval.py。新增 SQL Adapter 仍需真实隔离企业环境的合同测试。[R9]', 'small')

page(16, '交接信息、待办与来源', '本次交付为 PDF 设计参考；没有执行企业 SQL、变更数据库、启用 Worker 或修改生产配置。')
table(['交接项', '完成接入所需内容'], [
    ('内部团队提供', '内核 / 协议 / 网关合同；正式表字典与样例聚合；新闻与业务域映射；指标定义；批准主题目录；窗口关闭 / 修订 / 快照协议；只读权限与预算。'),
    ('项目需要实现', 'TDSQL SqlWarehouseClient；直接 SQL Reader Port 及工厂注册；滚动 release 绑定；查询回执；时长精度与真实基线样本映射。'),
    ('共同验证', '本文第 15 页验收矩阵；字段源表达式逐项签认；正文对象读取与撤稿 / 限权；规模与长稳；画像质量和运营使用效果。'),
], [100, W-100])
sub('本次文件生成与验证调用链')
para('上游：当前仓库 DDL / Port / 服务实现、项目背景、用户确认及两份官方资料。\n转换：生成脚本内的页级字段表、SQL 模板与说明 → ReportLab（内嵌中文字体）→ 本 PDF。\n验证：pypdf 提取文字与页数 / 必需关键词检查 → Poppler 渲染所有页面 → 人工式逐页视觉核对。\n配置：PDF_CJK_REGULAR、PDF_CJK_BOLD 可覆盖字体路径；仅用于文档生成，不读取 .env。\n下游：可供内部字段映射和评审；未完成事项见上表。')
sub('仓库依据（以当前工作区源码为准）')
refs = [
    '[R1] agent/app/sql_assistant/warehouse.py；agent/docs/enterprise-tdsql-warehouse-contract.md',
    '[R2] agent/app/modules/news_storage/store.py、tag_store.py；agent/docs/news-storage-database-reference.md',
    '[R3] agent/app/modules/reader_profiles/warehouse.sql、build.py、warehouse.py',
    '[R4] agent/app/modules/reader_profiles/contracts.py、service.py、factory.py',
    '[R5] agent/app/modules/news_storage/realtime.sql、realtime_002.sql 至 realtime_005.sql；agent/docs/wallclock-realtime-readers.md',
    '[R6] agent/app/modules/hot_news/scoring/hot_score.py；analysis/hot_news_enrichment.py；persistence/hot_news.py（后两项同属 modules/hot_news）',
    '[R7] agent/docs/enterprise-audience-implementation.md；agent/docs/local-behavior-tags.md',
    '[R8] agent/app/modules/knowledge/tier_lifecycle*.py；agent/docs/tiered-knowledge-lifecycle.md',
    '[R9] agent/tests/；agent/app/config.py；DemoMD/PROJECT_CONTEXT.md',
]
for ref in refs:
    para(ref, 'small')
sub('公开技术资料（2026-10-25 查阅）')
for text, url in [
    ('[E1] 腾讯云《TDSQL-A PostgreSQL版 产品简介》，产品概述更新于 2022-04-14；仅支持技术方向，不证明内部版本。', 'https://main.qcloudimg.com/raw/document/product/pdf/1378_54432_cn.pdf'),
    ('[E2] 腾讯云《TDSQL PostgreSQL版 创建只读账号》，更新于 2024-09-14；实际内部授权另行确认。', 'https://cloud.tencent.com/document/product/1129/78793'),
]:
    story.append(Paragraph(escape(text) + '<br/><link href="' + url + '" color="#007F82">' + url + '</link>', styles['small']))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.line(38, A4[1]-34, A4[0]-38, A4[1]-34)
    canvas.setFont('CN', 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(38, A4[1]-26, 'NEWSAGENT  /  热点新闻、用户标签与 TDSQL')
    canvas.drawRightString(A4[0]-38, A4[1]-26, '2026-10-25  ·  接入设计参考')
    canvas.line(38, 37, A4[0]-38, 37)
    canvas.drawString(38, 23, '现有实现与建议合同分别标注  |  非腾讯内部物理数据字典')
    canvas.drawRightString(A4[0]-38, 23, f'{doc.page:02d} / 16')
    canvas.restoreState()


if __name__ == '__main__':
    OUT.parent.mkdir(parents=True, exist_ok=True)
    QA.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, rightMargin=38, leftMargin=38,
                            topMargin=49, bottomMargin=49, title='NewsAgent 热点新闻与用户标签 - TDSQL 接入表结构',
                            author='NewsAgent 项目', subject='现有表结构、企业 SQL 合同、用户兴趣聚合与分层向量召回')
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    reader = PdfReader(str(OUT))
    extracted = [page.extract_text() or '' for page in reader.pages]
    if len(extracted) != 16:
        raise RuntimeError(f'Expected 16 pages; got {len(extracted)}. Fix overflow before delivery.')
    for i, title in enumerate(page_titles):
        compact = ''.join(extracted[i].split())
        if ''.join(title.split()) not in compact:
            raise RuntimeError(f'Page {i+1} heading missing: {title}')
    combined = '\n'.join(extracted)
    for token in ['news_reader_population', 'news_reader_topic', 'snapshot_v1', '720', '24', 'TDSQL', 'SqlWarehouseClient']:
        if token not in combined:
            raise RuntimeError(f'Missing required text: {token}')
    (QA / 'extracted.txt').write_text(combined, encoding='utf-8')
    summary = {'pages': len(reader.pages), 'bytes': OUT.stat().st_size,
               'sha256': hashlib.sha256(OUT.read_bytes()).hexdigest(),
               'headings_verified': True, 'required_text_verified': True,
               'render_review': 'pending', 'output': str(OUT)}
    (QA / 'validation.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
