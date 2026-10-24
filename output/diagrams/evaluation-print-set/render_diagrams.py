"""Render the writing and retrieval evaluation plans as A4 landscape PNG/SVG.

Input: verified REPORT_PILOT.md / RETRIEVAL_RESUME.md and the corresponding chats.
Transform: explicit node/arrow layout, local Chinese fonts, measured text bounds.
Core: node(), arrow(), centered(); dependency: Pillow, no network/model calls.
Outputs: 3508x2480 PNG at 300 DPI, SVG, screen preview. Run this file with Python.
No inference, retrieval experiment, scores, credentials or production changes.
"""
from pathlib import Path
from html import escape
import math
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
W, H = 1440, 1018
PW, PH = 3508, 2480
S = PW / W
FONT = '/System/Library/Fonts/STHeiti Light.ttc'
BOLD = '/System/Library/Fonts/STHeiti Medium.ttc'
INK, MUTED, LINE = '#173447', '#435461', '#718492'
TEAL, FILL, WHITE = '#126B66', '#F4F7F8', '#FFFFFF'
im = Image.new('RGB', (PW, PH), WHITE)
draw = ImageDraw.Draw(im)
svg = []


def rect(x, y, w, h, fill=WHITE, stroke=LINE, radius=12):
    draw.rounded_rectangle((round(x*S), round(y*S), round((x+w)*S), round((y+h)*S)),
                          radius=round(radius*S), fill=fill, outline=stroke, width=round(1.2*S))
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.2"/>')


def centered(cx, cy, value, size=20, color=INK, bold=False, limit=None):
    font = ImageFont.truetype(BOLD if bold else FONT, round(size*S))
    box = draw.textbbox((0, 0), value, font=font)
    if limit is not None:
        assert box[2]-box[0] <= limit*S, (value, box, limit)
    draw.text((cx*S-(box[2]-box[0])/2-box[0], cy*S-(box[3]-box[1])/2-box[1]), value, font=font, fill=color)
    svg.append(f'<text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="central" '
               f'font-family="STHeiti, Heiti SC, Arial Unicode MS, sans-serif" font-size="{size}" '
               f'font-weight="{600 if bold else 400}" fill="{color}">{escape(value)}</text>')


def arrow(points, color=LINE, width=1.8):
    draw.line([(round(x*S), round(y*S)) for x,y in points], fill=color, width=round(width*S), joint='curve')
    x,y = points[-1]
    px,py = points[-2]
    a = math.atan2(y-py,x-px)
    tip = [(x,y), (x-9*math.cos(a)+4*math.sin(a),y-9*math.sin(a)-4*math.cos(a)),
           (x-9*math.cos(a)-4*math.sin(a),y-9*math.sin(a)+4*math.cos(a))]
    draw.polygon([(round(u*S),round(v*S)) for u,v in tip], fill=color)
    svg.append(f'<polyline points="{" ".join(f"{x},{y}" for x,y in points)}" fill="none" stroke="{color}" stroke-width="{width}"/>')
    svg.append(f'<polygon points="{" ".join(f"{x},{y}" for x,y in tip)}" fill="{color}"/>')


def node(x, y, w, h, title, lines=(), accent=False, small=False):
    rect(x,y,w,h,fill=FILL if accent else WHITE,stroke=TEAL if accent else LINE)
    title_y = y+30 if lines else y+h/2
    centered(x+w/2,title_y,title,22 if not small else 20,bold=True,limit=w-26)
    if lines:
        first = y+60
        for i,line in enumerate(lines):
            centered(x+w/2,first+i*24,line,18 if not small else 17,color=MUTED,limit=w-24)


def begin(title, subtitle, stages):
    global im, draw, svg
    im = Image.new('RGB', (PW, PH), WHITE)
    draw = ImageDraw.Draw(im)
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="297mm" height="210mm" viewBox="0 0 {W} {H}">',
           f'<title>{escape(title)}</title>', f'<desc>{escape(subtitle)}</desc>',
           f'<rect width="{W}" height="{H}" fill="white"/>']
    centered(720,64,title,36,bold=True)
    centered(720,113,subtitle,21,color=MUTED,limit=1320)
    for x,stage in zip((55,515,975),stages):
        rect(x,164,410,58,fill=FILL,stroke=LINE,radius=9)
        centered(x+205,193,stage,24,bold=True,limit=380)
    arrow([(473,193),(506,193)],TEAL,2.2)
    arrow([(933,193),(966,193)],TEAL,2.2)


def column(x, cards):
    for y,h,card in zip((253,382,511,668),(100,100,127,127),cards):
        title,lines,*extra = card
        node(x,y,410,h,title,lines,accent=bool(extra and extra[0]))
    for y1,y2 in ((353,381),(482,510),(638,667)):
        arrow([(x+205,y1),(x+205,y2)])


def finish(stem, main, detail, status, footer):
    rect(55,839,1330,119,fill=FILL,stroke=LINE,radius=10)
    centered(720,869,main,21,bold=True,limit=1270)
    centered(720,902,detail,18,limit=1270)
    centered(720,934,status,18,color=MUTED,limit=1270)
    centered(720,987,footer,15,color=MUTED,limit=1340)
    svg.append('</svg>')
    im.save(OUT/f'{stem}.png',dpi=(300,300))
    (OUT/f'{stem}.svg').write_text('\n'.join(svg),encoding='utf-8')
    im.resize((1440,1018),Image.Resampling.LANCZOS).save(OUT/f'{stem}-preview.png')
    print(OUT/f'{stem}.png')


begin('数据驱动写作 · 评测执行链路',
      '目标：检验 Research、Writer、Reviewer 在给定材料上的事实、数字与报告质量',
      ('① 冻结材料与标准答案','② 运行真实报告生成链','③ 人工核对与对照评分'))
column(55,[
    ('确认报告任务与新闻素材',('腾讯／OpenAI 两个开发主题','核对来源、正文版本和证据引用')),
    ('你：审核标准答案与口径',('必需事实、数值公式及单位容差','确认禁止推断和报告可用标准')),
    ('prepare 冻结本轮实验',('固定输入、标签、Prompt 和模型','保存源码、依赖和样本分母','隐藏 gold，不将答案发送给模型')),
    ('配置真实模型与调用预算',('DeepSeek Flash 原生接口','固定并发、超时与调用上限','不同方案使用独立运行目录'),True),
])
column(515,[
    ('Research：整理事实与证据',('从给定缓存材料提取主张与引用','本轮不额外调用在线搜索')),
    ('Outline → Writer → Assemble',('生成提纲并逐章写作','拼装完整报告，保留阶段输出')),
    ('Reviewer：首轮审校',('检查稿件并输出问题与建议','当前不自动修订或批准','审校意见不等于独立事实答案')),
    ('保存产物并生成评分表',('逐阶段输出、版本、耗时与错误','生成 human-review 模板','技术执行完成不代表报告正确'),True),
])
column(975,[
    ('你：核对报告与审校结果',('逐项判断事实、数值语境与严重错误','填写原句证据、可用性和审校判断')),
    ('score：确定性计算指标',('Decimal 校验数值、单位与容差','保留固定分母，汇总人工判断')),
    ('候选 Prompt → 同条件重跑',('更新 Prompt 版本，保留当前方案','候选同样独立人工评分','compare 检查可比条件后计算差值')),
    ('形成可追溯评测报告',('输出 JSON／Markdown 与逐例错误','事实覆盖、数值通过、报告可用率','另报严重错误及审校检出／误报'),True),
])
finish('writing-evaluation-execution',
       '主指标：Research 必需事实覆盖率 · Writer 必需数值通过率 · 完整报告可用率',
       '人工判断事实与数值语境；Reviewer 检出／误报针对自然生成稿，使用各自适用分母。',
       '当前：两主题开发材料已准备；标准答案审核、真实推理与质量评分待完成。',
       '对应窗口：简历润色(评测)  ·  A4 横向／300 DPI  ·  本轮不评估实时搜索、修订闭环或 Temporal 恢复')

begin('热／温／冷关联检索 · 评测执行链路',
      '目标：给定真实源新闻，检验历史关联报道的排序质量及完整检索链耗时',
      ('① 冻结任务与运行条件','② 同条件检索与重排对照','③ 候选盲审与指标计算'))
column(55,[
    ('确认真实源新闻检索任务',('绑定 source_news_id 与规范正文','排除源新闻自召回及未来证据')),
    ('按事件分组并隔离数据',('同事件及更正链不跨 split','现有 16 条预审材料不等于独立 N')),
    ('固定三层实现与资产身份',('冻结 Embedding、索引、正文时点','固定实际后端及生命周期门禁','静态索引实验不等于生产迁层验收')),
    ('冻结 Top5 配置与计时协议',('candidate_limit＝20，related_limit＝5','固定硬件、并发、缓存和预热','保留失败、超时及实际返回数量'),True),
])
node(515,253,410,100,'源标题＋摘要 → Query Embedding',('源正文、发布时间另供业务重排','同一输入、语料、模型与索引'))
arrow([(720,353),(720,381)])
node(515,382,410,100,'三层查询 → 过滤去重 → RRF',('按实际链执行门禁及必要的补搜','按新闻粒度融合，补全候选元信息'))
arrow([(720,482),(720,514)])
centered(720,536,'只改变是否加入业务重排',19,color=TEAL,limit=410)
arrow([(720,549),(720,559),(612,559),(612,575)])
arrow([(720,559),(827,559),(827,575)])
node(515,576,195,91,'A：仅 RRF',('保留融合后排序',),small=True)
node(730,576,195,91,'B：加入重排',('主体／事件／时效',),small=True)
arrow([(612,667),(612,689),(720,689),(720,712)])
arrow([(827,667),(827,689),(720,689)])
node(515,713,410,82,'保存排序、错误并测 B 的耗时',('候选池盲审用两组真实运行输出',),accent=True)
column(975,[
    ('合并 A／B 候选并匿名盲审',('隐藏方案名称和原始排序','按统一标准评 0–3 级相关性')),
    ('独立双审，分歧仲裁',('补判新增候选，确保 Top5 全部有标签','未标注不能当作不相关的 0 分')),
    ('冻结 test＋qrels → 离线评分',('score_retrieval_judgments.py','计算 pooled exp2 nDCG@5','两组使用同一固定查询集合')),
    ('填写 N、A、B、T 及适用边界',('N：有正例的独立测试查询数','A／B：nDCG@5 均值；T：B 链 P95','另报事件组数量、超时和失败率'),True),
])
finish('retrieval-evaluation-execution',
       'nDCG@5 为人工候选池口径；本对照衡量业务重排收益，不单独证明分层或 RRF 收益。',
       'P95 含源内容读取、Embedding、三层查询／门禁、RRF、候选补全及重排；不含下游 LLM。',
       '当前：16 条起始材料及评分入口已准备；正式 A／B 检索、候选盲审与性能测试待完成。',
       '对应窗口：简历润色(评测) (2)  ·  A4 横向／300 DPI  ·  不用 ANN-only 或缺重排的 API 耗时代填 T')


