"""Render the one-page operations architecture update from local audit results."""
from pathlib import Path
from math import atan2, cos, sin, pi
import json
import re

from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT/'output/operations-design-sync-20261016'
RESULTS = json.loads((AUDIT/'results.json').read_text(encoding='utf-8'))
checks = {x['name']: x for x in RESULTS['checks']}
front = (AUDIT/'audience_frontend.txt').read_text(encoding='utf-8')
match = re.search(r'^(?:#|ℹ) pass (\d+)\s*$', front, re.M)
assert match and checks['audience_frontend']['status']=='passed'
front_count = int(match.group(1))
unit_count = checks['writing_unit']['test_count']
assert unit_count==26 and front_count==15 and not RESULTS['frozen_file_drift']
OUT = ROOT/'output/pdf/NewsAgent_多智能体运营架构更新_2026-10-16.pdf'
OUT.parent.mkdir(parents=True, exist_ok=True)
pdfmetrics.registerFont(TTFont('CN', '/System/Library/Fonts/Supplemental/Arial Unicode.ttf'))
W,H=landscape(A3)
c=canvas.Canvas(str(OUT),pagesize=(W,H))
c.setTitle('NewsAgent 新闻运营架构更新 2026-10-16')
c.setAuthor('NewsAgent')
INK='#17324D'; MUTED='#52667C'; BLUE='#2563B6'; TEAL='#147D75'; PURPLE='#7654B5'; AMBER='#94621C'

def text(x,y,value,size=12,color=INK):
    c.setFillColor(colors.HexColor(color)); c.setFont('CN',size); c.drawString(x,y,value)

def para(x,y,value,width,size=11,leading=18,color=MUTED):
    for p in value.split('\n'):
        line=''
        for ch in p:
            if line and pdfmetrics.stringWidth(line+ch,'CN',size)>width:
                text(x,y,line,size,color); y-=leading; line=ch
            else: line+=ch
        text(x,y,line,size,color); y-=leading
    return y

def box(x,y,w,h,fill='#FFFFFF',stroke=None,r=10):
    c.setFillColor(colors.HexColor(fill));c.setStrokeColor(colors.HexColor(stroke or fill))
    c.roundRect(x,y,w,h,r,fill=1,stroke=bool(stroke))

def arrow(x1,y1,x2,y2,color=BLUE):
    c.setStrokeColor(colors.HexColor(color));c.setFillColor(colors.HexColor(color));c.setLineWidth(1.7)
    c.line(x1,y1,x2,y2);a=atan2(y2-y1,x2-x1);p=c.beginPath();p.moveTo(x2,y2)
    p.lineTo(x2-7*cos(a-pi/7),y2-7*sin(a-pi/7));p.lineTo(x2-7*cos(a+pi/7),y2-7*sin(a+pi/7));p.close()
    c.drawPath(p,fill=1,stroke=0)

c.setFillColor(colors.HexColor('#F4F7FB'));c.rect(0,0,W,H,fill=1,stroke=0)
M=32;CW=W-64
text(M,H-42,'热点分析 × 主题受众：为人工推送决策准备依据',26)
text(M,H-65,'NewsAgent  |  架构、企业价值、评测与优化路线同步版  |  2026-10-16',11,MUTED)

box(M,H-143,CW,58,'#173958')
text(M+16,H-109,'本期业务目标',11,'#A8DCD7')
text(M+140,H-109,'这篇新闻为什么值得关注？对应哪些兴趣主题？是否存在通知资格受众？',17,'#FFFFFF')
text(M+16,H-131,'当前新增能力是受众预览；事件跟进与研究写作继续保留。',10,'#DBE9F3')

box(M,H-214,430,52,'#E8F1FC')
text(M+13,H-184,'企业已有画像系统',13,BLUE)
text(M+144,H-184,'访问行为 → 兴趣标签与通知资格',12,INK)
text(M+13,H-203,'本项目按版本查询聚合结果，逐用户画像与名单留在企业侧。',10,MUTED)
box(M+445,H-214,CW-445,52,'#E8F3EF')
text(M+460,H-184,'能力定位',13,TEAL)
text(M+540,H-184,'数据分析与决策辅助 Agent + 确定性受众服务',13,INK)
text(M+460,H-203,'受众服务不调用 LLM，不是新增 Agent，也不自动选择标签。',10,MUTED)

gap=17;nw=(CW-gap*4)/5;ny=H-367;nh=127
nodes=[
 ('热点报告','指标与基线\n证据 + 模型解释','已有分析基础',BLUE),
 ('人工选主题','主题 / 细分\n包含 / 排除 / any/all','已有目录与页面',BLUE),
 ('聚合受众','Port → 企业 / 本地替身\n匹配人数 / 资格人数','已有接口预览',TEAL),
 ('快照校验','身份 / 版本 / 窗口\n水位 / 完整性 / 时效','Python 确定性核验',TEAL),
 ('人工评估','考虑进入评估 / 观察\n正式审批与投递待接','不是发送授权',AMBER),
]
for i,(title,body,tag,col) in enumerate(nodes):
    x=M+i*(nw+gap);box(x,ny,nw,nh)
    text(x+13,ny+99,title,16,col);para(x+13,ny+72,body,nw-26,12,21)
    text(x+13,ny+14,tag,10,col)
    if i<4: arrow(x+nw+2,ny+66,x+nw+gap-2,ny+66,'#7A8FA6')

box(M,H-465,CW,77,'#FFFFFF')
text(M+15,H-414,'按需研究写作',14,PURPLE)
text(M+165,H-414,'Research → 研究人审 → 提纲人审 → Writer → Reviewer → 终稿人审 → 独立 CMS 发布',12,INK)
text(M+15,H-439,'目标组合卡',12,TEAL)
text(M+165,H-439,'新闻新进展 + 数据证据 + 主题组合 + 受众/资格快照 + 限制 + 人工下一步',12,INK)
text(M+165,H-455,'完整组合卡、事实增量及受众快照自动带入写作任务仍待实现。',9,MUTED)

pw=(CW-16)/2;py=H-655;ph=169
box(M,py,pw,ph,'#E8F1FC')
box(M+pw+16,py,pw,ph,'#E8F3EF')
text(M+16,py+140,'企业价值：先验证运营准备成本',16,BLUE)
para(M+16,py+114,'减少跨系统查数、整理背景和核验受众的重复劳动。\n比较：现有 BI/画像工具、加通用模型、NewsAgent。\n计入：准备、阅读、核验、改写、补救和维护时间。',pw-32,12,23)
text(M+16,py+21,'受众人数不是 UV、送达量或点击预测；提效与收益尚待试点。',10,MUTED)
rx=M+pw+32
text(rx,py+140,'评测：保留原集，单独增加受众层',16,TEAL)
para(rx,py+114,'八月写作 27 题 + 合成主线运营判断：各保留原版。\n新增：主题映射、组合去重、新闻绑定、有效期与零资格。\n旧 AudienceRule 不等于新主题目录，需版本化映射。',pw-32,12,23)
text(rx,py+31,f'本轮：写作 {unit_count} 项通过；受众前端 {front_count} 项通过；八月哈希一致。',10,TEAL)
text(rx,py+13,'主线校验缺依赖受阻；后端未重跑；未调用模型或企业接口。',10,AMBER)

text(M,H-685,'下一步',14,INK)
steps=['联调真实目录与资格口径','组合研判卡与事实增量','持久审批 / 投递 / 回执','人工标签与真实岗位试点']
sw=(CW-3*12)/4
for i,label in enumerate(steps):
    x=M+i*(sw+12);box(x,H-737,sw,36,'#FFFFFF')
    text(x+11,H-724,str(i+1)+'  '+label,11,INK)
text(M,52,'当前默认关闭；不改生产配置。源说明与本轮命令、哈希、日志：agent/docs/news-operations-agent-design.md',9,MUTED)
text(M,32,'图示不将预览、资格人数或校验通过等同于正式批准；真实推送与收益须独立联调和验收。',10,MUTED)
text(W-67,18,'1 / 1',9,MUTED)
c.showPage();c.save();print(OUT)
