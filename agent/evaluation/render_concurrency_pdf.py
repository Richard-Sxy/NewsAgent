"""Create an offline Chinese visual guide to concurrency simulation.

Inputs: archived load JSON, current design/tool documentation. No HTTP, database,
model calls or .env loading. Run from repo root with ReportLab installed:
    python agent/evaluation/render_concurrency_pdf.py
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
from html import escape
import json
from pathlib import Path

from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "output/pdf/NewsAgent_高并发与多租户场景模拟图解.pdf"
W, H = A4
M = 40
CW = W - 2 * M
TOTAL = 8
NAVY = HexColor("#17324B")
INK = HexColor("#29465C")
GRAY = HexColor("#62798C")
TEAL = HexColor("#087F8C")
BLUE = HexColor("#397CC0")
AMBER = HexColor("#AA691C")
RED = HexColor("#C65352")
GREEN = HexColor("#2B866D")
PALE = HexColor("#F1F5F8")
LINE = HexColor("#DAE4EC")
TENANT_COLORS = [TEAL, BLUE, HexColor("#8262AD"), HexColor("#D68B32"),
                 HexColor("#C46278"), HexColor("#599865"), HexColor("#679EAD"),
                 HexColor("#9B8C45"), HexColor("#56758F"), HexColor("#8B7D9C")]


def load_sources():
    sources = {}
    for n in (10, 100):
        path = ROOT / f"agent/evaluation/results/conversation-load-{n}-existing-reports-20261009.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        assert value["users"] == value["attempted_turns"] == len(value["samples"]) == n
        assert Counter(s["status"] for s in value["samples"]) == Counter(value["statuses"])
        assert value["clean_completed_turns"] == value["completed_turns"]
        assert value["runtime"]["model_route"] == "glm-5.3-flash"
        sources[n] = value
    paths = [
        "agent/docs/multi-tenant-concurrency-design.md",
        "agent/docs/conversation-concurrency.md",
        "agent/tools/conversation_load.py",
        "agent/docs/hot-news-concurrency.md",
        "agent/examples/hot_news_concurrency_demo.py",
    ]
    for relative in paths:
        assert (ROOT / relative).is_file(), relative
    assert sources[100]["completed_turns"] == 66 and sources[100]["errors"] == {"model_unavailable": 34}
    return sources


class ConcurrencyGuide:
    def __init__(self, output, sources, font, bold_font):
        pdfmetrics.registerFont(TTFont("CN", str(font), subfontIndex=0))
        pdfmetrics.registerFont(TTFont("CN-Bold", str(bold_font), subfontIndex=0))
        self.c = canvas.Canvas(str(output), pagesize=A4, pageCompression=1)
        self.c.setTitle("NewsAgent 高并发与多租户场景模拟图解")
        self.c.setAuthor("NewsAgent")
        self.c.setSubject("100虚拟用户、发压方式、配额时间轴、租户公平、历史证据与测试入口")
        self.sources = sources
        self.page = 0

    def rect(self, x, y, w, h, color=PALE, radius=9, stroke=None):
        self.c.setFillColor(color)
        self.c.setStrokeColor(stroke or color)
        self.c.roundRect(x, H-y-h, w, h, radius, fill=1, stroke=bool(stroke))

    def text(self, value, x, y, size=11, color=INK, bold=False):
        font = "CN-Bold" if bold else "CN"
        if x + pdfmetrics.stringWidth(str(value), font, size) > W-M+1:
            raise ValueError(f"page {self.page} text too wide: {value}")
        self.c.setFont(font, size)
        self.c.setFillColor(color)
        self.c.drawString(x, H-y-size*.85, str(value))

    def para(self, value, x, y, w, size=10.5, color=INK, bold=False, maxh=80, leading=None):
        style = ParagraphStyle("cn", fontName="CN-Bold" if bold else "CN", fontSize=size,
                               leading=leading or size*1.5, textColor=color, wordWrap="CJK")
        p = Paragraph(escape(str(value)).replace("\n", "<br/>"), style)
        _, h = p.wrap(w, H)
        if h > maxh+.5 or y+h > H-48:
            raise ValueError(f"page {self.page}: paragraph overflow {h}/{maxh}: {value[:70]}")
        p.drawOn(self.c, x, H-y-h)
        return h

    def line(self, x1, y1, x2, y2, color=LINE, width=1):
        self.c.setStrokeColor(color)
        self.c.setLineWidth(width)
        self.c.line(x1, H-y1, x2, H-y2)

    def arrow(self, x1, y1, x2, y2, color=TEAL):
        self.line(x1, y1, x2, y2, color, 1.4)
        p = self.c.beginPath()
        p.moveTo(x2, H-y2)
        if abs(x2-x1) >= abs(y2-y1):
            d = 1 if x2 >= x1 else -1
            p.lineTo(x2-5*d, H-y2+3)
            p.lineTo(x2-5*d, H-y2-3)
        else:
            d = 1 if y2 >= y1 else -1
            p.lineTo(x2-3, H-y2+5*d)
            p.lineTo(x2+3, H-y2+5*d)
        p.close()
        self.c.setFillColor(color)
        self.c.drawPath(p, fill=1, stroke=0)

    def badge(self, value, x, y, color=TEAL):
        width = pdfmetrics.stringWidth(value, "CN", 9)+16
        self.rect(x, y, width, 22, Color(color.red, color.green, color.blue, alpha=.10), 5)
        self.text(value, x+8, y+6, 9, color)
        return width

    def start(self, section, title, sub):
        self.page += 1
        self.c.bookmarkPage(f"p{self.page}")
        self.c.addOutlineEntry(title, f"p{self.page}", level=0)
        self.text(f"NEWSAGENT / {section}", M, 33, 9, TEAL, True)
        self.text(title, M, 65, 23, NAVY, True)
        self.para(sub, M, 106, CW, 10.5, GRAY, maxh=34)

    def end(self):
        self.line(M, H-39, W-M, H-39, LINE, .7)
        self.text("NewsAgent · 场景模拟指南 · 2026-10-10", M, H-28, 8, GRAY)
        self.text(f"{self.page:02d} / {TOTAL:02d}", W-M-39, H-28, 8, GRAY)
        self.c.showPage()

    def note(self, title, body, y, h=80, color=TEAL):
        self.rect(M, y, CW, h)
        self.rect(M, y, 4, h, color, 1)
        self.text(title, M+16, y+14, 12, color, True)
        self.para(body, M+16, y+36, CW-32, 10.3, maxh=h-40)

    def box(self, x, y, w, h, title, body, color=TEAL):
        self.rect(x, y, w, h)
        self.text(title, x+12, y+12, 12, color, True)
        self.para(body, x+12, y+34, w-24, 10, maxh=h-39)

    def grid(self, x, y, assignments, cell=14, gap=3):
        assert len(assignments)==100
        for i, group in enumerate(assignments):
            self.rect(x+(i%10)*(cell+gap), y+(i//10)*(cell+gap), cell, cell, TENANT_COLORS[group], 2)

    def cover(self):
        self.start("VISUAL GUIDE", "高并发场景，如何模拟？", "以 100 个虚拟用户为例：看清谁在发请求、谁在排队、谁占用模型，以及如何判断测试结果。")
        x=M
        for label,color in (("真实历史数据",TEAL),("可控模拟假设",BLUE),("待实现设计",AMBER)):
            x+=self.badge(label,x,151,color)+9
        self.grid(M, 199, [i//10 for i in range(100)], 17, 3)
        self.text("每个方块 = 1 个用户", M, 409, 10, GRAY)
        self.text("100", 285, 202, 56, TEAL, True)
        self.text("虚拟用户", 285, 269, 16, NAVY, True)
        self.para("每人拥有独立身份、会话和请求 UUID。可由异步压测程序发起，无需手工打开 100 个浏览器窗口。", 285, 304, 255, 11, maxh=72)
        self.text("先把三个数字分开", M, 459, 15, NAVY, True)
        bw=(CW-20)/3
        for i,(n,label,body) in enumerate((("100","在线 / 在途用户","客户侧连接与请求数量"),("10","模型执行槽示例","服务侧真实同时调用上限"),("90","首次排队示例","单调用场景中其余请求等待"))):
            x=M+i*(bw+10)
            self.rect(x,491,bw,125)
            self.text(n,x+13,505,32,TEAL,True)
            self.text(label,x+13,546,11,NAVY,True)
            self.para(body,x+13,571,bw-26,9.5,GRAY,maxh=32)
        self.note("这本图解的阅读顺序", "第2-3页：准备用户与租户；第4-6页：选择负载、观察排队与公平；第7页：看真实历史结果；第8页：按步骤复现。", 645, 88)
        self.para("10 个槽与 90 个排队是教学假设，尚未作为本项目全局调度配置上线。", M, 751, CW, 9.5, AMBER, maxh=30)
        self.end()

    def setup(self):
        self.start("01 / TEST TOPOLOGY", "用一台发压机模拟 100 位客户", "先准备用户与合成证据，再同步释放请求；服务端指标必须独立采集。")
        self.box(M,155,CW,65,"01 预置测试数据","每租户准备专属新闻/报告标记；每用户独立会话。记录版本和请求ID，保留原业务历史。")
        self.arrow(W/2,222,W/2,237)
        self.box(M,242,CW,64,"02 等待起跑屏障","创建100个异步用户任务，全部等待同一Event；会话创建不计入消息突发延迟。",BLUE)
        self.arrow(W/2,307,W/2,323)
        self.box(M,327,CW,66,"03 同时放行 / 按速率放行","屏障一次放行测突发；定时按到达率放行测持续负载。记录每个请求的起止、状态和工具结果。",BLUE)
        self.arrow(W/2,394,W/2,410)
        self.rect(M,417,CW,170,PALE)
        self.text("后续双API验收拓扑 / HTTP与SSE链路",M+14,431,12,NAVY,True)
        self.box(M+14,459,132,104,"可信测试网关","验证身份与租户成员关系\n剥离伪造角色头",TEAL)
        self.box(M+176,459,132,104,"API A / API B","鉴权、会话单飞\nPython Agent循环",TEAL)
        self.box(M+338,459,163,104,"共享依赖与观测","PG事实源、模型接口\n工具 / Temporal\n采集资源占用",TEAL)
        self.arrow(M+148,510,M+173,510)
        self.arrow(M+310,510,M+335,510)
        self.note("两种测试，分别得出结论", "可控本地模型Port：固定延迟/错误，验证配额、取消、幂等。真实模型端点：验证实际容量与质量，消耗接口额度。",613,91)
        self.para("多副本用于验证全局额度不会翻倍。当前压测脚本覆盖单租户HTTP/SSE；测试网关、多租户夹具和共享调度仍待补齐。",M,727,CW,10,AMBER,maxh=48)
        self.end()

    def layouts(self):
        self.start("02 / TENANT LAYOUTS", "同样 100 人，换四种分组", "租户是组织/团队边界，用户是成员。不同布局回答不同问题，不能只替换一个 Header 就算完整验收。")
        specs=[("1 租户 × 100 用户",[0]*100,"单租户基线","是否接住突发、排队是否有界。"),
               ("10 租户 × 10 用户",[i//10 for i in range(100)],"均衡竞争","每租户等待、份额和证据均正确。"),
               ("100 租户 × 1 用户",[i%10 for i in range(100)],"大量小租户","每个用户属于独立租户；检查无饥饿。"),
               ("80 + 4 × 5 用户",[0]*80+[j for j in range(1,5) for _ in range(5)],"大租户施压","小租户能继续获得服务。")]
        for i,(title,assign,goal,body) in enumerate(specs):
            y=158+i*137
            self.rect(M,y,CW,124)
            self.text(title,M+14,y+12,13,NAVY,True)
            self.grid(M+16,y+37,assign,6,2)
            self.text(goal,M+128,y+40,13,TEAL,True)
            self.para(body,M+128,y+64,CW-146,10.5,maxh=32)
            if i==2:
                self.para("本图重复用色；100个方块各属不同租户。",M+128,y+98,CW-146,8.5,GRAY,maxh=18)
        self.note("隔离检查要有可识别的数据", "给A租户新闻加A专属标记，给B加B标记；同时查询，返回内容和模型上下文都只能出现当前授权作用域的证据。空结果不能代替正确取数验证。",721,73)
        self.end()

    def workloads(self):
        self.start("03 / LOAD SHAPES", "选一种发压方式，再看一组指标", "下面曲线是负载示意。虚拟用户数与请求到达率分别配置；慢响应不能自动降低目标到达率。")
        for i,(title,subtitle) in enumerate((("突发：100人同时点击","测瞬间受理、排队峰值与超时"),("梯度：10 / 25 / 50 / 100人","每档稳定观察，再升下一档"),("持续：固定到达率，15-30分钟","测队列增长、资源泄漏和恢复"))):
            y=155+i*155
            self.rect(M,y,CW,141)
            self.text(title,M+14,y+12,13,NAVY,True)
            cx,cy,cw,ch=M+25,y+52,210,60
            self.line(cx,cy+ch,cx+cw,cy+ch,GRAY)
            self.line(cx,cy,cx,cy+ch,GRAY)
            self.text("时间",cx+cw-23,cy+ch+8,8.5,GRAY)
            self.text("发压强度",cx+7,cy-11,8.5,GRAY)
            if i==0:
                self.line(cx,cy+ch,cx+13,cy+ch,BLUE,2)
                self.line(cx+13,cy+ch,cx+13,cy+5,BLUE,2)
                self.line(cx+13,cy+5,cx+22,cy+5,BLUE,2)
                self.line(cx+22,cy+5,cx+22,cy+ch,BLUE,2)
                self.line(cx+22,cy+ch,cx+cw,cy+ch,BLUE,2)
            elif i==1:
                lastx,lasty=cx,cy+ch
                for j,height in enumerate((8,18,33,55)):
                    xx=cx+j*cw/4
                    self.line(lastx,lasty,xx,cy+ch-height,BLUE,2)
                    self.line(xx,cy+ch-height,xx+cw/4,cy+ch-height,BLUE,2)
                    lastx,lasty=xx+cw/4,cy+ch-height
            else:
                for j in range(14):
                    xx=cx+8+j*14
                    self.line(xx,cy+ch,xx,cy+20,BLUE,1.5)
            self.para(subtitle,M+260,y+49,CW-276,11,maxh=38)
            self.para(("用起跑屏障释放100条消息。", "分别记录每档完成率与P95；遇到错误先定位。", "按目标速率产生请求；设置最大在途数，超出也计入拒绝统计。")[i],M+260,y+90,CW-276,9.5,GRAY,maxh=38)
        self.note("最容易误读的两个数", "100个在途用户不是100 RPS；SSE首个进度事件不是有效答复。成功/失败/拒绝分别统计，同时给出每租户队列等待和端到端延迟。",641,87)
        self.para("已有 --rounds 是多次同步突发。固定到达率、持续时长和多租户分布参数尚未进入现有脚本。",M,747,CW,9.5,AMBER,maxh=32)
        self.end()

    def timeline(self):
        self.start("04 / CAPACITY TIMELINE", "100 个请求，10 个执行槽", "教学假设：每请求只调用模型一次，每次固定2秒；全局10槽、无失败和额外开销。")
        x0=M+58; step=(CW-64)/10
        self.text("时间 / 秒",M,157,10,GRAY)
        for k in range(11):
            self.text(str(k*2),x0+k*step-(4 if k else 0),157,8.8,GRAY)
        self.text("模型槽",M,192,10,NAVY,True)
        for slot in range(10):
            y=219+slot*25
            self.text(f"{slot+1:02d}",M+14,y+6,10,GRAY)
            for wave in range(10):
                self.rect(x0+wave*step+1,y,step-3,20,TENANT_COLORS[wave],3)
                self.text(str(wave*10+slot+1),x0+wave*step+6,y+6,8,white)
        self.text("格内数字 = 请求编号；每列完成10个请求。",M,482,10,GRAY)
        self.text("首批开始时",M,519,12,NAVY,True)
        self.rect(M+124,511,174,38,TEAL)
        self.text("10个执行中",M+144,524,13,white,True)
        self.rect(M+309,511,206,38,PALE)
        self.text("90个等待",M+333,524,13,AMBER,True)
        self.box(M,575,247,94,"容量计算","10槽 ÷ 2秒 = 5次调用/秒\n100次调用约需20秒",TEAL)
        self.box(M+261,575,254,94,"持续发压会怎样？","若持续8次/秒，饱和后每秒\n新增约3个排队请求。",AMBER)
        self.note("真实 Agent 一轮可能调用模型多次", "规划时按模型调用数、token和工具耗时计算。模型槽只覆盖一次模型调用；等待SQL、Workflow或退避时归还。此图不能直接预测真实聊天的总耗时。",693,90,BLUE)
        self.end()

    def fairness(self):
        self.start("05 / FAIR COMPETITION", "大租户施压，小租户还能前进", "模拟：租户A有80人，B/C/D/E各5人；请求成本相同、租户等权、模型总窗口10。")
        self.grid(M,155,[0]*80+[j for j in range(1,5) for _ in range(5)],11,3)
        labels=[("A · 80人",0),("B · 5人",1),("C · 5人",2),("D · 5人",3),("E · 5人",4)]
        for j,(label,idx) in enumerate(labels):
            self.rect(M+178,155+j*28,12,12,TENANT_COLORS[idx],2)
            self.text(label,M+201,156+j*28,11,NAVY)
        self.para("观察小租户的首次派发时间、等待P95和成功率，不能只看总吞吐。",M+340,164,175,11,maxh=65)
        self.text("如果A先排满共享FIFO",M,335,13,NAVY,True)
        for i in range(10):
            x=M+i*(CW/10)
            self.rect(x,365,CW/10-5,36,TEAL,5)
            self.text("A",x+18,377,12,white,True)
        self.para("首批可能全是A，后来的小租户等待拉长。",M,416,CW,10.5,GRAY,maxh=25)
        self.text("按租户轮转，再选该租户用户",M,463,13,NAVY,True)
        for i in range(10):
            idx=i%5; x=M+i*(CW/10)
            self.rect(x,493,CW/10-5,36,TENANT_COLORS[idx],5)
            self.text("ABCDE"[idx],x+18,505,12,white,True)
        self.para("示意首批为A/B/C/D/E各2次；后续按有需求的租户继续轮转。",M,544,CW,10.5,GRAY,maxh=27)
        self.note("硬上限、权重、借用各管一件事", "硬上限限制最大占用；权重决定竞争份额；其他租户空闲时可借用额度，仍不突破硬上限。本文示意的租户上限不成为瓶颈，也不静态预占每租户2槽。",589,95)
        self.para("以上是待实现的共享公平调度设计。跨2个以上API/Worker测试总额度，不能每个进程各建10槽。取消后远端调用未知时，保留保守占用。",M,711,CW,10.5,AMBER,maxh=54)
        self.end()

    def evidence(self):
        self.start("06 / MEASURE THE OUTCOME", "历史实测：连接成功，业务仍会失败", "2026-10-09归档；单租户100用户，真实glm-5.3-flash，普通对话与只读已有热点各占一半。")
        self.badge("历史实测 / 非本次新压测",M,150,TEAL)
        self.rect(M,193,CW,125)
        self.text("100个SSE连接均HTTP 200",M+16,209,16,NAVY,True)
        self.rect(M+16,249,(CW-32)*.66,30,GREEN,3)
        self.rect(M+16+(CW-32)*.66,249,(CW-32)*.34,30,RED,3)
        self.text("66个completed",M+28,260,12,white,True)
        self.text("34个业务失败",M+16+(CW-32)*.66+10,260,12,white,True)
        self.text("completed且无失败/拒绝工具；失败码为model_unavailable。",M+16,293,9.5,GRAY)
        y=352
        cols=[M,M+230,M+364]
        self.text("指标",cols[0],y,11,NAVY,True)
        self.text("10用户基线",cols[1],y,11,NAVY,True)
        self.text("100用户突发",cols[2],y,11,NAVY,True)
        rows=[("完成 / 尝试","10 / 10","66 / 100"),
              ("首个SSE进度P95","0.032秒","0.794秒"),
              ("成功轮次总耗时P95","10.108秒","11.097秒"),
              ("历史一致 / 终态重放","各10 / 10","各100 / 100"),
              ("其他用户 / 租户读被拒绝","各10 / 10","各100 / 100")]
        for i,row in enumerate(rows):
            yy=y+35+i*35
            self.line(M,yy-10,W-M,yy-10)
            for j,v in enumerate(row): self.text(v,cols[j],yy,10,INK)
        self.note("报告至少输出这些维度", "尝试、受理、拒绝、排队过期、业务成功、工具目标达成；总体与每租户队列等待/P95；服务端模型许可峰值、HTTP错误分类、DB等待和内存。",594,86)
        self.para("34个model_unavailable不能直接归因为429；历史通过包含失败轮次。跨租户读取被拒绝是越权检查，尚未验证多租户公平性。",M,706,CW,10.5,AMBER,maxh=52)
        self.para("来源：evaluation/results/conversation-load-{10,100}-existing-reports-20261009.json；均位于agent/。",M,770,CW,8.3,GRAY,maxh=19)
        self.end()

    def reproduce(self):
        self.start("07 / REPRODUCE & NEXT", "按这条路线准备下一次测试", "先核对依赖、数据与模型类型，再运行已有演示；多租户调度测试按设计阶段逐步补齐。")
        self.text("在agent/目录，使用已安装项目依赖的Python",M,150,11,NAVY,True)
        self.rect(M,179,CW,172,PALE)
        commands=[
            ("A. 无网络模拟：单次运行内的新闻批量并发",TEAL),
            ("python -m examples.hot_news_concurrency_demo",INK),
            ("  --concurrency 3 --delay-ms 200",INK),
            ("B. 单租户客户压测：先10用户，确认后再100",TEAL),
            ("python tools/conversation_load.py --users 10",INK),
            ("  --transport sse --workload mixed",INK),
            ("  --output evaluation/results/load-10.json",INK),
        ]
        for i,(line,color) in enumerate(commands):
            self.text(line,M+14,191+i*20,9.7,color,i in (0,3))
        self.para("上方命令分行展示参数，运行时各命令合为一行。B默认连接127.0.0.1:28000隔离演示API，可能调用真实计费模型；--users改为100时另选输出文件。",M,365,CW,9.8,GRAY,maxh=47)
        self.text("从现有脚本，到真正多租户模拟",M,434,14,NAVY,True)
        stages=[("准备身份与证据","统一各API鉴权；各租户合成canary；记录镜像/模型/策略版本。"),
                ("实现共享准入与配额","queued独立于processing；PG原子绑定；同会话最多一个活跃请求。"),
                ("补充发压与故障场景","10×10、100×1、80+4×5；至少2副本；取消、重放、429、崩溃。")]
        for i,(title,body) in enumerate(stages):
            yy=466+i*65
            self.badge(f"{i+1:02d}",M,yy,TEAL)
            self.text(title,M+43,yy+1,11,NAVY,True)
            self.para(body,M+43,yy+24,CW-43,9.5,maxh=30)
        self.line(M,677,W-M,677)
        self.text("生成链、校验与资料入口",M,690,11,NAVY,True)
        self.para("归档JSON + 设计/工具文档 → load_sources()核对统计 → ConcurrencyGuide / ReportLab绘图 → 嵌入中文字体的8页PDF。参数：--output / --font / --bold-font。布局验收：Poppler逐页渲染、文字与页数检查。",M,714,CW,9,maxh=44,leading=13)
        self.para("资料：agent/docs/下的multi-tenant-concurrency-design.md、conversation-concurrency.md、hot-news-concurrency.md。TODO：共享调度/多租户夹具/真实配额与SLO验收。",M,762,CW,8.2,GRAY,maxh=30,leading=11)
        self.end()

    def render(self):
        for method in (self.cover,self.setup,self.layouts,self.workloads,self.timeline,self.fairness,self.evidence,self.reproduce):
            method()
        assert self.page == TOTAL
        self.c.save()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=DEFAULT_OUTPUT)
    parser.add_argument("--font",type=Path,default=Path("/System/Library/Fonts/STHeiti Light.ttc"))
    parser.add_argument("--bold-font",type=Path,default=Path("/System/Library/Fonts/STHeiti Medium.ttc"))
    args=parser.parse_args()
    sources=load_sources()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    ConcurrencyGuide(args.output,sources,args.font,args.bold_font).render()
    print(json.dumps({"output":str(args.output.resolve()),"pages":TOTAL,
                      "sha256":sha256(args.output.read_bytes()).hexdigest(),"offline":True},ensure_ascii=False))


if __name__=="__main__":
    main()
