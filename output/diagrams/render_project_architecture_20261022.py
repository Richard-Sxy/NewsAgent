"""Render the conversation's confirmed OSS boundary and audited architecture.

Chain: confirmed user context + reviewed repository contracts -> fixed visual
summary -> render_architecture (ReportLab and embedded STHeiti) -> one-page PDF.
Config: --output; A3 landscape; no model, database, network, or app config access.
QA entry: pypdf page/text checks and Poppler PNG rendering + visual review.
Business TODO: OSS/version adapter, reference-only ledger, enterprise contracts,
and production tiered-index quality/capacity validation. No runtime is changed.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[2]
W, H = landscape(A3)
INK = "#173647"
MUTED = "#5D7280"
BLUE = "#266AA3"
TEAL = "#117E72"
AMBER = "#996119"
RULE = "#D8E3E8"


class Diagram:
    def __init__(self, output: Path):
        pdfmetrics.registerFont(TTFont("CN", "/System/Library/Fonts/STHeiti Light.ttc", subfontIndex=0))
        pdfmetrics.registerFont(TTFont("CNB", "/System/Library/Fonts/STHeiti Medium.ttc", subfontIndex=0))
        self.c = canvas.Canvas(str(output), pagesize=(W, H), pageCompression=1)
        self.c.setTitle("NewsAgent 项目架构：企业数仓、新闻 OSS 与向量 RAG")
        self.c.setAuthor("NewsAgent")
        self.c.setSubject("单页中文架构摘要；腾讯OSS为全文事实源；目标与当前落地边界；2026-10-22")

    def text(self, value, x, top, size=14, color=INK, bold=False, maxw=None):
        font = "CNB" if bold else "CN"
        if maxw is not None and pdfmetrics.stringWidth(value, font, size) > maxw:
            raise ValueError(f"Text exceeds its allocated width: {value}")
        self.c.setFillColor(HexColor(color))
        self.c.setFont(font, size)
        self.c.drawString(x, H - top - size, value)

    def lines(self, values, x, top, size=14, leading=23, color=MUTED, maxw=None):
        for i, value in enumerate(values):
            self.text(value, x, top + i * leading, size, color, maxw=maxw)

    def box(self, x, top, w, h, fill="#FFFFFF", stroke=RULE, radius=12):
        self.c.setFillColor(HexColor(fill))
        self.c.setStrokeColor(HexColor(stroke))
        self.c.setLineWidth(1)
        self.c.roundRect(x, H - top - h, w, h, radius, fill=1, stroke=1)

    def arrow(self, points, color=BLUE, width=1.7, dashed=False):
        self.c.saveState()
        self.c.setStrokeColor(HexColor(color))
        self.c.setFillColor(HexColor(color))
        self.c.setLineWidth(width)
        if dashed:
            self.c.setDash(4, 3)
        path = self.c.beginPath()
        path.moveTo(points[0][0], H - points[0][1])
        for x, y in points[1:]:
            path.lineTo(x, H - y)
        self.c.drawPath(path)
        self.c.setDash()
        x, y = points[-1]
        a, b = points[-2]
        angle = math.atan2(y - b, x - a)
        size = 7
        p = self.c.beginPath()
        p.moveTo(x, H - y)
        for offset in (0.48, -0.48):
            xx = x - size * math.cos(angle + offset)
            yy = y - size * math.sin(angle + offset)
            p.lineTo(xx, H - yy)
        p.close()
        self.c.drawPath(p, fill=1, stroke=0)
        self.c.restoreState()

    def tag(self, value, x, top, w, fill, color):
        self.box(x, top, w, 23, fill, fill, 6)
        self.text(value, x + 9, top + 3, 11.5, color, bold=True, maxw=w - 18)

    def icon_database(self, x, top, color=BLUE):
        c = self.c
        c.saveState()
        c.setStrokeColor(HexColor(color))
        c.setLineWidth(1.6)
        c.ellipse(x, H - top - 9, x + 24, H - top, fill=0, stroke=1)
        c.line(x, H - top - 4.5, x, H - top - 24)
        c.line(x + 24, H - top - 4.5, x + 24, H - top - 24)
        c.arc(x, H - top - 28.5, x + 24, H - top - 19.5, startAng=180, extent=180)
        c.arc(x, H - top - 18, x + 24, H - top - 9, startAng=180, extent=180)
        c.restoreState()


def render_architecture(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    d = Diagram(output)
    c = d.c
    c.setFillColor(HexColor("#F7FAFC"))
    c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(HexColor("#153647"))
    c.rect(0, H - 106, W, 106, fill=1, stroke=0)
    d.text("NEWSAGENT  /  SYSTEM ARCHITECTURE", 40, 16, 10.5, "#9BC8D1", bold=True)
    d.text("NewsAgent 项目架构", 40, 36, 29, "#FFFFFF", bold=True)
    d.text("全文留在腾讯 OSS · 数字来自企业数仓 · 本项目维护可重建的检索索引", 40, 78, 14, "#D5E8EC")
    d.text("2026.10.22  |  目标架构与当前边界", 866, 28, 12, "#D5E8EC")

    # Entry and orchestration are shared across both business lanes.
    d.box(40, 122, 1110, 43, "#E8F0F5", "#E8F0F5", 9)
    d.text("运营工作台 / 聊天 / 定时触发", 57, 134, 13.8, INK, bold=True)
    d.arrow([(303, 143), (337, 143)], INK)
    d.text("FastAPI 鉴权 + 受控工具路由", 350, 134, 13.8, INK, bold=True)
    d.arrow([(616, 143), (650, 143)], INK)
    d.text("业务服务执行；长任务由 Temporal 持久编排", 664, 134, 13.8, INK, bold=True)

    d.text("01  企业已有数据", 40, 181, 12, MUTED, bold=True)
    d.text("02  NewsAgent 核心能力", 340, 181, 12, MUTED, bold=True)
    d.text("03  分析与运营交付", 890, 181, 12, MUTED, bold=True)

    # Warehouse lane.
    d.box(40, 207, 260, 139, "#EEF5FC", "#C9DBED")
    d.icon_database(58, 226, BLUE)
    d.text("企业数仓", 94, 223, 20, BLUE, bold=True)
    d.lines(["企业侧采集、清洗、去重与聚合", "曝光 / 点击 / 阅读 / 互动", "窗口指标 + 历史基线 + 内部榜单"], 58, 262, 13, 23, INK, 225)
    d.arrow([(300, 277), (340, 277)], BLUE)

    d.box(340, 207, 520, 139, "#FFFFFF", "#C9DBED")
    d.text("看数：只读取数与确定性计算", 360, 223, 19, BLUE, bold=True)
    d.lines(["受限 Text2SQL / 聚合接口 → 水位、完整性与版本检查", "Python / SQL 计算指标、历史对比、热度与排行", "企业原榜保留原排名与来源；原始用户行为留在企业侧"], 360, 261, 14, 23, INK, 480)
    d.arrow([(860, 277), (890, 277)], BLUE)

    # OSS and RAG lane, with two distinct build/serve paths.
    d.box(40, 371, 260, 160, "#EAF7F3", "#BDDDD5")
    d.icon_database(58, 391, TEAL)
    d.text("腾讯新闻 OSS", 94, 388, 20, TEAL, bold=True)
    d.lines(["按 news_id 提供新闻全文", "绑定内容版本或可核验摘要", "通过企业内容服务 / Adapter 读取"], 58, 428, 13, 23, INK, 225)
    d.tag("唯一全文事实源", 58, 499, 128, "#D4EDE5", TEAL)
    d.arrow([(300, 428), (340, 428)], TEAL)
    d.arrow([(340, 493), (300, 493)], TEAL)
    d.text("回源", 306, 474, 10, TEAL)

    d.box(340, 371, 520, 160, "#FFFFFF", "#BDDDD5")
    d.text("查证据：新闻 RAG 与向量索引", 360, 388, 19, TEAL, bold=True)
    d.lines(["建索引：指定版本全文 → 清洗切片 → Embedding → 向量", "查证据：查询编码 → 召回 / 去重 / 重排 → 按需回源"], 360, 427, 13.5, 24, INK, 480)
    d.tag("热", 360, 492, 38, "#FCE2D7", "#A74C2D")
    d.tag("温", 405, 492, 38, "#FFF0CB", "#997027")
    d.tag("冷", 450, 492, 38, "#DDEAF8", BLUE)
    d.text("news_id + chunk_id + 内容 / 模型版本", 502, 496, 12.1, MUTED, maxw=340)
    d.arrow([(860, 443), (875, 443), (875, 320), (890, 320)], TEAL)

    d.box(890, 207, 260, 168, "#F0F2FB", "#D4D9EE")
    d.text("证据驱动的分析", 908, 223, 19, "#555B91", bold=True)
    d.lines(["指标快照 + 新闻证据", "LLM 理解、解释与生成", "结构 / 数字 / 引用确定性校验"], 908, 262, 13, 24, INK, 224)
    d.tag("模型服务：推理 + Embedding", 908, 344, 222, "#E2E6F6", "#555B91")
    d.arrow([(1020, 375), (1020, 397)], "#555B91")
    d.box(890, 397, 260, 134, "#FFFFFF", "#D4D9EE")
    d.text("运营可用的结果", 908, 413, 19, "#555B91", bold=True)
    d.lines(["新闻问答 / 热点报告 / 跟进建议", "可选：研究写作、聚合受众预览", "发布须人工审核；不自动推送"], 908, 453, 12.8, 23, INK, 224)

    d.text("04  项目自有存储与反馈闭环", 40, 553, 12, MUTED, bold=True)
    items = [
        (40, "PostgreSQL", ["会话、任务、索引台账", "版本、反馈、审批；目标不存全文"]),
        (324, "Redis", ["缓存、事件推送", "辅助加速，不作为事实源"]),
        (608, "S3 / MinIO", ["报告、评测等生成产物", "与腾讯新闻全文 OSS 职责分开"]),
        (892, "反馈与评测", ["人工标注 → 冻结集 → 对照实验", "生产变更经人工审批后生效"]),
    ]
    for x, title, body in items:
        d.box(x, 577, 258, 90, "#FFFFFF", RULE)
        d.text(title, x + 15, 589, 16, INK, bold=True)
        d.lines(body, x + 15, 621, 11.9, 19, MUTED, 228)

    d.box(40, 686, 1110, 101, "#FFF6E7", "#E8D2AC", 10)
    d.text("当前落地边界", 57, 698, 14.5, AMBER, bold=True)
    d.lines([
        "01  当前 PG 仍保存正文副本；改为 OSS 版本引用与索引台账、接入真实企业接口，仍需实现与验收。",
        "02  本地 BGE 是只读快照；三层召回已做隔离实验，生命周期已有代码，均不等于生产三层 RAG 已上线。",
        "03  本地数仓使用真实公开新闻与模拟行为指标；新增内容没有行为数据时，不能自动成为热点候选。",
    ], 57, 725, 12.3, 19, "#765C35", 1076)
    d.text("统一关联键：news_id    |    版本一致性：内容版本 + 切片 / Embedding 版本    |    主目标：问答检索 + 相关新闻跟进", 40, 807, 10.5, MUTED, maxw=1060)
    d.text("1 / 1", 1118, 807, 10.5, MUTED)
    c.showPage()
    c.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output/pdf/NewsAgent_项目架构_数仓与OSS_RAG_2026-10-22.pdf")
    args = parser.parse_args()
    render_architecture(args.output)
    print(args.output)


if __name__ == "__main__":
    main()
