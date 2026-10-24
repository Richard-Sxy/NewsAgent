"""Render the reviewed execution plan as an A4 landscape PNG and editable SVG.

Input: the query-semantics execution flow explained in this chat.
Transform: explicit node/arrow layout, local Chinese fonts, measured text bounds.
Core: node(), arrow(), centered(); dependency: Pillow, no network/model calls.
Outputs: 3508x2480 PNG at 300 DPI, SVG, screen preview. Run this file with Python.
No training, inference, scores, credentials or production changes are included.
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
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="297mm" height="210mm" viewBox="0 0 {W} {H}">',
       '<title>查询解析与 LoRA SFT 执行链路</title>',
       '<desc>本机审核和冻结数据，台式机训练并对照推理，本机校验评分。当前没有真实 A、B 成绩。</desc>',
       f'<rect width="{W}" height="{H}" fill="white"/>']


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


centered(720,64,'查询解析与 LoRA SFT · 执行链路',36,bold=True)
centered(720,113,'目标：减少时间、指标及筛选条件的误解与遗漏',21,color=MUTED)

for x,title in ((55,'① 本机：准备与审核'),(515,'② 台式机：训练与推理'),(975,'③ 本机：校验与评分')):
    rect(x,164,410,58,fill=FILL,stroke=LINE,radius=9)
    centered(x+205,193,title,24,bold=True,limit=380)
arrow([(473,193),(506,193)],TEAL,2.2)
arrow([(933,193),(966,193)],TEAL,2.2)

# Stage 1: the user reviews business meaning; labels are kept out of inference.
node(55,253,410,100,'收集问题与候选答案',('真实匿名坏例／Hy4 辅助构建', '现有 24 条仅作开发检查'))
arrow([(260,353),(260,381)])
node(55,382,410,100,'人工标注与独立复核',('你确认业务问题和标准答案', '校准时间、指标、数量与筛选'))
arrow([(260,482),(260,510)])
node(55,511,410,127,'按需求分组并冻结数据',('隔离 train／validation／test', '同一需求及其改写不跨集合', '另标 Golden／Fresh／High-risk'))
arrow([(260,638),(260,667)])
node(55,668,410,127,'传递执行包与训练数据',('冻结 Prompt、Schema、时间和策略', '被评模型只看测试输入', '测试标准答案留在本机评分'),accent=True)

# Stage 2: choose the adapter with validation before the paired test runs.
node(515,253,410,100,'训练集 → LoRA SFT',('使用同一 Hunyuan-7B-Instruct 基座', '只用人工确认的目标训练'))
arrow([(720,353),(720,381)])
node(515,382,410,100,'验证集选择并冻结 LoRA',('选择训练参数和 checkpoint', '独立测试集不参与调参'))
arrow([(720,482),(720,514)])
centered(720,536,'同一测试输入 · 同一 Prompt 与推理设置',18,color=TEAL,limit=410)
arrow([(720,549),(720,559),(612,559),(612,575)])
arrow([(720,559),(827,559),(827,575)])
node(515,576,195,91,'未微调基座',('生成逐题预测 A',),small=True)
node(730,576,195,91,'基座＋LoRA',('生成逐题预测 B',),small=True)
arrow([(612,667),(612,689),(720,689),(720,712)])
arrow([(827,667),(827,689),(720,689)])
node(515,713,410,82,'回传两次完整运行目录',('预测、模型版本、配置与失败记录',),accent=True)

# Stage 3: compare raw semantic slots before the Python execution guard.
node(975,253,410,100,'核验运行证据',('检查数据、版本、配置和预测覆盖', '确认训练组与测试组隔离'))
arrow([(1180,353),(1180,381)])
node(975,382,410,100,'独立标准答案 → 确定性评分',('score／compare 逐题比较语义槽位', '不以模型自评分作为标准答案'))
arrow([(1180,482),(1180,510)])
node(975,511,410,127,'得到基座与 LoRA 对照结果',('A：基座查询语义全匹配率', 'B：LoRA 查询语义全匹配率', '另报高风险错误放行率'),accent=True)
arrow([(1180,638),(1180,667)])
node(975,668,410,127,'依据真实结果填写简历',('保留样本量、错误案例和版本', '条件不足时，A／B 保持待填写', '评测结果不触发自动上线'))

rect(55,839,1330,119,fill=FILL,stroke=LINE,radius=10)
centered(720,869,'主指标：全部 13 个语义槽位正确的正常查询数 ÷ 正常查询总数',22,bold=True,limit=1270)
centered(720,902,'超时与非法输出保留在分母；拒绝／澄清题单列评分；两次测试条件一致。',19,limit=1270)
centered(720,934,'当前状态：执行计划与脚本已准备；人工审核、独立测试集及台式机真实实验待完成。',18,color=MUTED,limit=1270)
centered(720,987,'NewsAgent  ·  查询理解评测  ·  A4 横向／300 DPI  ·  此图为执行计划，不代表已完成 SFT',15,color=MUTED)
svg.append('</svg>')
im.save(OUT/'query-semantics-sft-execution.png',dpi=(300,300))
(OUT/'query-semantics-sft-execution.svg').write_text('\n'.join(svg),encoding='utf-8')
im.resize((1440,1018),Image.Resampling.LANCZOS).save(OUT/'preview.png')
print(OUT/'query-semantics-sft-execution.png')
