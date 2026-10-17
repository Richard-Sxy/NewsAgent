"""Draw a single-page, vector illustrated architecture poster from frozen v7 evidence.

From the repository root, run with the bundled Python/ReportLab runtime. Inputs
and validation are shared with render_tencent_warehouse_architecture_pdf.py;
no enterprise, database, model, or vector-store connection is opened.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph
from html import escape

from render_tencent_warehouse_architecture_pdf import DATASET, file_hash, load_evidence

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "output/pdf/NewsAgent_腾讯新闻模拟数仓_一页架构总览.pdf"
W, H = landscape(A3)
INK = HexColor("#193A50")
MUTED = HexColor("#60778A")
GREEN = HexColor("#168574")
GREEN_BG = HexColor("#EEF8F2")
BLUE = HexColor("#367CCB")
BLUE_BG = HexColor("#EDF4FF")
ORANGE = HexColor("#BC7728")
ORANGE_BG = HexColor("#FFF4E5")
RED = HexColor("#C8585D")
GRAY = HexColor("#DAE4EC")


class WarehousePoster:
    def __init__(self, output: Path, evidence: dict, font: Path, bold_font: Path):
        pdfmetrics.registerFont(TTFont("PosterCN", str(font), subfontIndex=0))
        pdfmetrics.registerFont(TTFont("PosterBold", str(bold_font), subfontIndex=0))
        self.c = canvas.Canvas(str(output), pagesize=(W, H), pageCompression=1)
        self.c.setTitle("腾讯新闻模拟数仓 - 一页架构总览")
        self.c.setAuthor("NewsAgent")
        self.c.setSubject("单页矢量架构海报：合成场景、运营数仓、正文知识、Agent与撤稿治理")
        self.e = evidence

    def rect(self, x, y, w, h, fill=white, stroke=None, radius=12, dashed=False):
        self.c.saveState()
        self.c.setFillColor(fill)
        self.c.setStrokeColor(stroke or fill)
        self.c.setLineWidth(1.2)
        if dashed:
            self.c.setDash(5, 4)
        self.c.roundRect(x, H-y-h, w, h, radius, fill=1, stroke=bool(stroke))
        self.c.restoreState()

    def poly(self, points, fill, stroke=None):
        self.c.setFillColor(fill)
        self.c.setStrokeColor(stroke or fill)
        p = self.c.beginPath()
        p.moveTo(points[0][0], H-points[0][1])
        for x, y in points[1:]:
            p.lineTo(x, H-y)
        p.close()
        self.c.drawPath(p, fill=1, stroke=bool(stroke))

    def line(self, x1, y1, x2, y2, color=GRAY, width=1.4, dashed=False):
        self.c.saveState()
        self.c.setStrokeColor(color)
        self.c.setLineWidth(width)
        if dashed:
            self.c.setDash(5, 4)
        self.c.line(x1, H-y1, x2, H-y2)
        self.c.restoreState()

    def route(self, points, color=GREEN, dashed=False, width=2.2):
        for a, b in zip(points, points[1:]):
            self.line(*a, *b, color, width, dashed)
        x1,y1 = points[-2]
        x2,y2 = points[-1]
        theta = math.atan2(y2-y1,x2-x1)
        tip = [(x2,y2), (x2-9*math.cos(theta)+4*math.sin(theta),y2-9*math.sin(theta)-4*math.cos(theta)),
               (x2-9*math.cos(theta)-4*math.sin(theta),y2-9*math.sin(theta)+4*math.cos(theta))]
        self.poly(tip,color)

    def circle(self, x,y,r,fill=white,stroke=None,width=1.4):
        self.c.setFillColor(fill)
        self.c.setStrokeColor(stroke or fill)
        self.c.setLineWidth(width)
        self.c.circle(x,H-y,r,fill=1,stroke=bool(stroke))

    def ellipse(self,x,y,w,h,fill,stroke=None):
        self.c.setFillColor(fill)
        self.c.setStrokeColor(stroke or fill)
        self.c.ellipse(x,H-y-h,x+w,H-y,fill=1,stroke=bool(stroke))

    def text(self,value,x,y,size=12,color=INK,bold=False,width=None,center=False):
        value=str(value)
        font="PosterBold" if bold else "PosterCN"
        tw=pdfmetrics.stringWidth(value,font,size)
        if width is not None and tw>width+.5:
            raise ValueError(f"label too wide ({tw}/{width}): {value}")
        if center:
            x -= tw/2
        assert x>=25 and x+tw<W-25 and y+size<H-12, (value,x,y,tw)
        self.c.setFont(font,size)
        self.c.setFillColor(color)
        self.c.drawString(x,H-y-size*.85,value)

    def para(self,value,x,y,w,size=11,color=INK,maxh=50):
        style=ParagraphStyle("poster",fontName="PosterCN",fontSize=size,leading=size*1.45,
                             textColor=color,wordWrap="CJK")
        p=Paragraph(escape(value).replace("\n","<br/>"),style)
        _,h=p.wrap(w,H)
        if h>maxh+.5:
            raise ValueError(f"paragraph too tall ({h}/{maxh}): {value}")
        p.drawOn(self.c,x,H-y-h)

    def badge(self,value,x,y,color=GREEN,fill=GREEN_BG):
        w=pdfmetrics.stringWidth(value,"PosterCN",10.5)+20
        self.rect(x,y,w,24,fill,radius=6)
        self.text(value,x+10,y+7,10.5,color)

    def phone(self,x,y):
        self.rect(x+5,y+5,83,142,HexColor("#DDE7EF"),radius=17)
        self.rect(x,y,83,142,INK,radius=17)
        self.rect(x+6,y+12,71,116,white,radius=10)
        self.line(x+28,y+6,x+53,y+6,HexColor("#9CAFBF"),2.5)
        for i,color in enumerate((BLUE,GREEN,ORANGE)):
            self.rect(x+13,y+26+i*28,16,18,color,radius=4)
            self.line(x+36,y+31+i*28,x+68,y+31+i*28,GRAY,3)
            self.line(x+36,y+41+i*28,x+61,y+41+i*28,GRAY,2)
        self.circle(x+42,y+135,3,GRAY)
        # A hand tapping the news feed: purely illustrative, no event record.
        self.circle(x+80,y+85,12,HexColor("#F1CDB5"))
        self.rect(x+75,y+82,9,31,HexColor("#F1CDB5"),radius=4)
        self.line(x+90,y+83,x+101,y+78,ORANGE,1.2)
        self.line(x+90,y+90,x+103,y+92,ORANGE,1.2)

    def gear(self,cx,cy,r,color):
        points=[]
        for i in range(32):
            theta=i*math.pi/16
            length=r if i%4 in (0,1) else r*.78
            points.append((cx+length*math.cos(theta),cy+length*math.sin(theta)))
        self.poly(points,color)
        self.circle(cx,cy,r*.44,white)
        self.circle(cx,cy,r*.22,color)

    def database(self,x,y,w,h,color=GREEN):
        self.rect(x,y+9,w,h-16,HexColor("#CDE9E1") if color==GREEN else HexColor("#D6E6FC"),color,radius=2)
        self.ellipse(x,y,w,20,white,color)
        self.ellipse(x,y+h-20,w,20,HexColor("#CDE9E1") if color==GREEN else HexColor("#D6E6FC"),color)
        for offset in (h*.37,h*.63):
            self.line(x+2,y+offset,x+w-2,y+offset,color,.9)

    def warehouse(self):
        # Isometric walls, roof, database bays and a floor/conveyor turn the schema into a place.
        x,y,w,h=446,264,239,143
        self.poly([(x,y+h),(x+w,y+h),(x+w+32,y+h-15),(x+32,y+h-15)],HexColor("#CBDDD5"))
        self.poly([(x+w,y),(x+w+27,y-15),(x+w+27,y+h-15),(x+w,y+h)],HexColor("#94C6B3"),GREEN)
        self.rect(x,y,w,h,HexColor("#D5EDE3"),GREEN,radius=1)
        self.poly([(x-12,y),(x+w/2,y-47),(x+w+12,y)],HexColor("#72B69F"),GREEN)
        self.poly([(x+w/2,y-47),(x+w/2+25,y-61),(x+w+39,y-15),(x+w+12,y)],HexColor("#4C977F"),GREEN)
        self.text("运营数仓",x+w/2,y+15,25,GREEN,True,center=True)
        self.text("独立 PostgreSQL",x+w/2,y+49,12,INK,center=True)
        for offset in (22,92,162):
            self.database(x+offset,y+75,53,49)
        self.badge("news_sim_v7",x+66,y-29,GREEN,white)
        self.text("13 张表 · 5 个视图",x+w/2,y+128,12,GREEN,True,center=True)

    def gateway(self,x,y):
        self.rect(x,y,76,91,white,radius=10)
        self.rect(x+13,y+24,12,49,HexColor("#B4D9CC"),GREEN,radius=2)
        self.rect(x+56,y+24,12,49,HexColor("#B4D9CC"),GREEN,radius=2)
        self.line(x+22,y+31,x+61,y+31,GREEN,5)
        self.circle(x+40,y+13,17,white,GREEN)
        self.line(x+33,y+12,x+39,y+18,GREEN,2.5)
        self.line(x+39,y+18,x+49,y+6,GREEN,2.5)
        self.text("18 列",x+40,y+49,12,GREEN,True,center=True)

    def library(self,x,y):
        self.rect(x,y,50,58,HexColor("#C6D8F1"),BLUE,radius=3)
        for i,color in enumerate((BLUE,HexColor("#75A7DD"),HexColor("#B3C9E4"))):
            self.rect(x+6+i*13,y+10,10,40,color,radius=2)
            self.line(x+8+i*13,y+16,x+13+i*13,y+16,white,1)
        self.line(x+5,y+51,x+45,y+51,BLUE,3)

    def network(self,cx,cy):
        positions=[(cx-25,cy-18),(cx+22,cy-24),(cx+29,cy+19),(cx-20,cy+23),(cx,cy)]
        for a,b in ((0,1),(1,2),(2,3),(3,0),(0,4),(1,4),(2,4),(3,4)):
            self.line(*positions[a],*positions[b],BLUE,1.2)
        for index,(x,y) in enumerate(positions):
            self.circle(x,y,7 if index!=4 else 10,white,BLUE,1.7)

    def monitor(self,x,y):
        self.rect(x+4,y+5,178,128,HexColor("#D8E6F3"),radius=12)
        self.rect(x,y,178,124,INK,radius=10)
        self.rect(x+7,y+9,164,103,white,radius=5)
        self.text("热点排行 / 分析",x+82,y+18,13,BLUE,True,center=True)
        self.line(x+18,y+86,x+158,y+86,GRAY,1)
        for i,h in enumerate((20,34,28,45,54)):
            self.rect(x+24+i*24,y+85-h,13,h,BLUE if i!=4 else GREEN,radius=2)
        self.poly([(x+75,y+124),(x+104,y+124),(x+115,y+145),(x+64,y+145)],HexColor("#A5BACD"))
        self.rect(x+47,y+145,85,7,INK,radius=3)

    def dashboard(self,x,y):
        self.rect(x,y,236,120,BLUE_BG,BLUE,radius=12,dashed=True)
        self.text("运营看板 / 分析报告",x+16,y+13,16,BLUE,True)
        self.rect(x+17,y+43,48,57,white,radius=4)
        for i in range(4):
            self.line(x+25,y+54+i*10,x+54,y+54+i*10,GRAY,2)
        self.para("权威数字 + 正文证据\n模型表达 + 确定性校验\nv7 到共享运行仍待接入",x+80,y+44,143,11,maxh=55)

    def render(self):
        self.c.setFillColor(HexColor("#FCFDFE"))
        self.c.rect(0,0,W,H,fill=1,stroke=0)
        self.text("NEWSAGENT  /  ARCHITECTURE MAP",34,27,10,GREEN,True)
        self.text("腾讯新闻模拟数仓 · 一张图看懂",34,51,30,INK,True)
        self.text("合成场景 → 运营账本 → 查询与分析；正文提供证据，状态决定此刻能否使用。",34,96,13,MUTED)
        self.badge("实线：v7 已有数据链",34,128,GREEN,GREEN_BG)
        self.badge("蓝色虚线：v7 待接入关系",222,128,BLUE,BLUE_BG)
        self.badge("橙色：内容资格与撤稿治理",443,128,ORANGE,ORANGE_BG)

        # Regions are drawn before their internal pipeline and icons.
        self.rect(32,178,182,338,HexColor("#F1F5F8"),radius=16)
        self.rect(240,177,626,341,GREEN_BG,HexColor("#B4DACB"),radius=19)
        self.rect(920,204,236,293,BLUE_BG,BLUE,radius=17,dashed=True)
        self.rect(32,556,182,125,HexColor("#F1F5F8"),radius=16)
        self.rect(240,556,626,125,BLUE_BG,BLUE,radius=17,dashed=True)

        # Three lanes, with all target runtime connections explicitly dashed.
        self.route([(195,324),(259,324)],GREEN)
        self.route([(403,324),(432,324)],GREEN)
        self.route([(718,324),(755,324)],GREEN)
        self.route([(851,324),(915,324)],BLUE,True)
        self.text("权威指标",878,301,10.5,BLUE,center=True)
        self.route([(202,620),(267,620)],BLUE,True)
        self.route([(454,620),(533,620)],BLUE,True)
        self.route([(843,620),(899,620),(899,465),(915,465)],BLUE,True)
        self.text("正文证据",894,559,10.5,BLUE,center=True)
        self.route([(1038,501),(1038,547)],BLUE,True)
        # Current v7 lifecycle state feeds its SQL eligibility filter, not a live CMS/worker.
        self.route([(453,741),(453,724),(226,724),(226,537),(879,537),(879,350),(838,350)],ORANGE,width=1.8)
        self.text("状态 + 墓碑 → 过滤不可用内容",624,523,10.5,ORANGE,center=True)

        # Upstream illustrative consumption scene.
        self.text("合成业务场景",123,197,16,INK,True,center=True)
        self.phone(79,235)
        self.text("曝光 · 点击 · 消费 · 互动",123,393,11,INK,center=True)
        for i,label in enumerate(("推荐","栏目","搜索","热榜","推送","网页")):
            x=49+(i%3)*50
            y=417+(i//3)*29
            self.rect(x,y,45,22,white,radius=5)
            self.text(label,x+22,y+6,10.5,MUTED,center=True)
        self.text("只模拟聚合，不落用户明细",123,487,10,MUTED,center=True)

        self.text("v7 已实现：离线生成，可导入独立数仓",258,191,15,GREEN,True)
        self.rect(267,245,132,151,white,radius=13)
        self.text("Python 生成器",333,258,13,GREEN,True,center=True)
        self.gear(325,312,29,GREEN)
        self.gear(352,326,19,HexColor("#79B9A5"))
        self.text("五分钟 → 小时",333,354,11,INK,center=True)
        self.text("次数求和 / UV 去重",333,376,10,GREEN,center=True)
        self.warehouse()
        self.gateway(758,272)
        self.text("合格指标视图",798,381,12,GREEN,True,center=True)
        self.text("最新修订 + 完整窗口",798,405,9.6,MUTED,center=True)
        self.text("published / 无墓碑",798,422,9.6,MUTED,center=True)
        groups=(("内容字典","3 表",GREEN),("消费计量","3 表",GREEN),("质量 / 基线","2 表",GREEN),
                ("生命周期","3 表",ORANGE),("原榜留档","2 表",GREEN))
        for i,(label,count,color) in enumerate(groups):
            x=255+i*119
            self.rect(x,448,109,53,white,radius=9)
            self.text(label,x+54,458,11.5,color,True,center=True)
            self.text(count,x+54,480,10,MUTED,center=True)

        # Existing project capabilities shown as future mappings for this isolated v7 package.
        self.text("NewsAgent 工作台",1038,222,18,BLUE,True,center=True)
        self.monitor(949,259)
        self.text("SQL / Python 算数",1038,431,12,INK,True,center=True)
        self.text("模型解释与辅助写作",1038,455,11,INK,center=True)
        self.badge("v7 接入待完成",978,473,BLUE,white)
        self.dashboard(920,556)

        self.text("正文资产",123,571,14,INK,True,center=True)
        self.rect(50,601,44,44,white,GRAY,radius=4)
        self.rect(67,592,44,44,white,GRAY,radius=4)
        self.poly([(76,623),(85,610),(94,624)],HexColor("#9FBDE0"))
        self.circle(95,602,4,HexColor("#C6DCF1"))
        self.text("图文 / 视频 / QA",150,618,10.3,MUTED,center=True)
        self.text("v7 尚无正文与向量",123,656,10,MUTED,center=True)
        self.text("正文知识链：与 v7 的映射待接",257,571,14,BLUE,True)
        self.library(278,606)
        self.text("文档与切片",345,613,12,BLUE,True)
        self.text("来源 / 正文版本",345,638,10,MUTED)
        self.text("Embedding",493,599,10.2,BLUE,center=True)
        self.network(568,633)
        self.text("向量索引 = 检索目录",615,612,12,BLUE,True)
        self.text("找相关材料；相似度不当热度",615,638,10,MUTED)

        self.text("撤稿治理",34,701,15,ORANGE,True)
        self.text("先关闭通行闸门 → 再按版本清理 → 验证无残留",344,705,12,ORANGE)
        stages=(
            (32,"1  合成变更事件","撤稿 / 受限 / 更正",ORANGE,False),
            (320,"2  当前状态 + 墓碑","v7 视图已过滤；服务门禁待接",ORANGE,False),
            (608,"3  分目标清理任务","240 个 pending；按租户与版本绑定",ORANGE,False),
            (896,"4  真实执行与核验 · 待接","Milvus 分版删除 / BGE 屏蔽后重建",BLUE,True),
        )
        for index,(x,title,body,color,dashed) in enumerate(stages):
            self.rect(x,738,260,62,ORANGE_BG if index<3 else BLUE_BG,color if dashed else None,radius=10,dashed=dashed)
            self.text(title,x+12,750,12.3,color,True,width=236)
            self.para(body,x+12,775,236,10.1,MUTED,maxh=24)
            if index<3:
                self.route([(x+264,768),(x+282,768)],ORANGE if index<2 else BLUE,index==2,width=1.8)

        manifest=self.e["manifest"]
        rows=sum(value["rows"] for value in manifest["tables"].values())
        self.text(f"13 表 | 5 视图 | 240 篇合成新闻 | {rows:,} 行 | 2 租户 | 7 天 + 开放小时",34,815,10.5,INK)
        footer="公开业务背景模拟；未接企业实时流或真实清理。"
        self.text(footer,W-35-pdfmetrics.stringWidth(footer,"PosterCN",9.6),815,9.6,MUTED)
        self.c.showPage()
        self.c.save()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset",type=Path,default=DATASET)
    parser.add_argument("--output",type=Path,default=OUTPUT)
    parser.add_argument("--font",type=Path,default=Path("/System/Library/Fonts/STHeiti Light.ttc"))
    parser.add_argument("--bold-font",type=Path,default=Path("/System/Library/Fonts/STHeiti Medium.ttc"))
    args=parser.parse_args()
    evidence=load_evidence(args.dataset)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    WarehousePoster(args.output,evidence,args.font,args.bold_font).render()
    print({"output":str(args.output.resolve()),"pages":1,"sha256":file_hash(args.output)})


if __name__=="__main__":
    main()
