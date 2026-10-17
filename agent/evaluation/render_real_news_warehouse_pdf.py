"""Render a single-page Chinese warehouse map from validated v8 evidence."""

from __future__ import annotations

import argparse
from decimal import Decimal
from html import escape
import json
import math
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

W, H = landscape(A3)
INK, MUTED = HexColor("#183B50"), HexColor("#597487")
GREEN, BLUE, AMBER = HexColor("#167D68"), HexColor("#367AC2"), HexColor("#B4762C")
PALE_GREEN, PALE_BLUE, PALE_AMBER = (HexColor(value) for value in ("#EDF7F2", "#EDF4FC", "#FFF4E5"))
GRAY = HexColor("#D5E2E9")


class Diagram:
    def __init__(self, output: Path):
        pdfmetrics.registerFont(TTFont("CN", "/System/Library/Fonts/STHeiti Light.ttc", subfontIndex=0))
        pdfmetrics.registerFont(TTFont("CNB", "/System/Library/Fonts/STHeiti Medium.ttc", subfontIndex=0))
        self.c = canvas.Canvas(str(output), pagesize=(W, H), pageCompression=1)
        self.c.setTitle("真实新闻模拟数仓 - 规模、统计链路与后续Agent接口")
        self.c.setAuthor("NewsAgent")

    def box(self, x, y, w, h, fill=white, stroke=None, dashed=False):
        self.c.saveState()
        self.c.setFillColor(fill)
        self.c.setStrokeColor(stroke or fill)
        self.c.setLineWidth(1.2)
        if dashed:
            self.c.setDash(5, 4)
        self.c.roundRect(x, H-y-h, w, h, 12, fill=1, stroke=bool(stroke))
        self.c.restoreState()

    def text(self, value, x, y, size=12, color=INK, bold=False, center=False):
        self.c.setFillColor(color)
        self.c.setFont("CNB" if bold else "CN", size)
        if center:
            self.c.drawCentredString(x, H-y-size, str(value))
        else:
            self.c.drawString(x, H-y-size, str(value))

    def para(self, value, x, y, w, size=12, maxh=100, color=INK):
        p = Paragraph(escape(value).replace("\n", "<br/>"), ParagraphStyle(
            "body", fontName="CN", fontSize=size, leading=size*1.45,
            textColor=color, wordWrap="CJK"))
        _, height = p.wrap(w, H)
        if height > maxh:
            raise ValueError(f"diagram text overflow: {value}")
        p.drawOn(self.c, x, H-y-height)

    def arrow(self, x1, y1, x2, y2, color=GREEN, dashed=False):
        self.c.saveState()
        self.c.setStrokeColor(color)
        self.c.setFillColor(color)
        self.c.setLineWidth(2)
        if dashed:
            self.c.setDash(5, 4)
        self.c.line(x1, H-y1, x2, H-y2)
        p = self.c.beginPath()
        theta = math.atan2(y2-y1, x2-x1)
        p.moveTo(x2, H-y2)
        p.lineTo(x2-8*math.cos(theta)+4*math.sin(theta), H-(y2-8*math.sin(theta)-4*math.cos(theta)))
        p.lineTo(x2-8*math.cos(theta)-4*math.sin(theta), H-(y2-8*math.sin(theta)+4*math.cos(theta)))
        p.close()
        self.c.drawPath(p, stroke=0, fill=1)
        self.c.restoreState()

    def route(self, points, color=GREEN):
        self.c.saveState()
        self.c.setStrokeColor(color)
        self.c.setLineWidth(1.2)
        for (x1, y1), (x2, y2) in zip(points[:-2], points[1:-1]):
            self.c.line(x1, H-y1, x2, H-y2)
        self.c.restoreState()
        self.arrow(*points[-2], *points[-1], color)

    def database(self, x, y):
        self.box(x, y+10, 94, 65, PALE_GREEN, GREEN)
        self.c.setFillColor(white)
        self.c.setStrokeColor(GREEN)
        for offset in (0, 34, 64):
            self.c.ellipse(x, H-y-offset-18, x+94, H-y-offset, stroke=1, fill=offset == 0)


def render(dataset: Path, output: Path) -> None:
    manifest = json.loads((dataset / "manifest.json").read_text())
    validation = json.loads((dataset / "validation.json").read_text())
    observed = json.loads((dataset / "postgres-observed.json").read_text())
    for evidence in (validation, observed):
        if evidence.get("dataset_sha256") != manifest["dataset_sha256"] or evidence.get("status") != "passed":
            raise ValueError("diagram requires matching successful artifact and database evidence")
    counts = {name: meta["rows"] for name, meta in manifest["tables"].items()}
    if observed["counts"] != counts:
        raise ValueError("PostgreSQL row counts differ from exported artifacts")
    sample = observed["reference_metrics"]
    articles = validation["distinct_real_articles"]
    rows = validation["total_persisted_rows"]
    d = Diagram(output)
    d.c.setFillColor(HexColor("#FAFCFD"))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / REAL NEWS WAREHOUSE / V8", 32, 24, 10, GREEN, True)
    d.text("真实新闻 + 模拟行为：可查询的数仓底座", 32, 45, 29, INK, True)
    d.text(f"{articles:,} 篇不同真实新闻  ·  7 个业务栏目  ·  2 个隔离租户  ·  {rows:,} 行聚合与管理数据", 32, 92, 14, MUTED)
    d.box(32, 121, 1125, 29, PALE_AMBER)
    d.text("标题、正文、URL、发布时间来自公开缓存；点击与访问指标均为模拟值。此架构用于本项目演示。", 44, 128, 11.5, AMBER)

    for x, w, fill, color, title in (
        (32, 252, PALE_BLUE, BLUE, "1 真实内容"), (323, 252, PALE_GREEN, GREEN, "2 模拟统计"),
        (614, 252, PALE_GREEN, GREEN, "3 数仓存储"), (905, 252, PALE_BLUE, BLUE, "4 后续 Agent"),
    ):
        d.box(x, 178, w, 208, fill, color, dashed=x == 905)
        d.text(title, x+18, 193, 18, color, True)
    d.para("公开页面缓存\n661 篇原文，保留来源与正文摘要\n以真实 news_id 关联所有表", 50, 236, 216, 13, 66)
    d.para("科技 / 财经 / 体育 / 社会\n汽车 / 教育 / 娱乐\n栏目为演示标签，带来源说明", 50, 312, 216, 11.5, 55, MUTED)
    d.para("曝光 → 点击 → 详情访问\n允许搜索、外链等直接访问\n人数用临时集合并集去重", 341, 236, 216, 13, 66)
    d.para("无企业用户行为明细\n同一 seed 生成同一份数据\n新闻发布之前没有行为", 341, 312, 216, 11.5, 55, MUTED)
    d.database(694, 231)
    d.text("news_sim_v8", 740, 318, 16, GREEN, True, True)
    d.text("12 张表 · SQL / JSONL / 校验摘要", 740, 351, 10.5, MUTED, center=True)
    d.para("只读、参数化查询\n按租户 + news_id + 窗口取数\nPython 计算，模型解释", 923, 236, 216, 13, 66)
    d.para("已有 18 字段兼容视图\n访问 PV / UV 用独立统计接口\n本次完成底座，运行接入待做", 923, 312, 216, 11.5, 55, MUTED)
    for x1, x2, color, dashed in ((284, 315, GREEN, False), (575, 606, GREEN, False), (866, 897, BLUE, True)):
        d.arrow(x1, 276, x2, 276, color, dashed)

    d.text("五分钟、渠道都对齐小时；日 UV 另行去重", 32, 404, 17, INK, True)
    grains = (
        ("五分钟", "news_metric_5min", "完整历史桶 / 修订桶"),
        ("渠道小时", "news_metric_channel_hourly", "推荐、搜索、推送等"),
        ("新闻小时", "news_metric_hourly", "跨渠道统一口径"),
        ("新闻每日", "news_metric_daily", "跨小时去重 / 末日部分"),
        ("历史基线", "news_metric_baseline_hourly", "过去3天同小时 / 缺失NULL"),
    )
    for i, (name, table, note) in enumerate(grains):
        x = 32 + i*229
        d.box(x, 447, 207, 90, white, GRAY)
        d.text(name, x+15, 458, 14, GREEN, True)
        d.text(f"{counts[table]:,} 行", x+15, 482, 17, INK, True)
        d.text(note, x+15, 513, 9.8, MUTED)
        if i in (1, 2):
            d.arrow(x+207, 490, x+221, 490, GRAY)
    d.route([(136, 537), (136, 549), (593, 549), (593, 537)], GREEN)
    d.route([(593, 447), (593, 435), (1051, 435), (1051, 447)], GREEN)
    d.text("过去同小时 → 基线", 831, 410, 10, MUTED)

    d.box(32, 559, 752, 201, white, BLUE)
    d.text("一篇真实新闻，怎样得到业务统计？", 49, 574, 17, BLUE, True)
    d.para(sample["title"], 49, 605, 710, 14, 45)
    d.text(f"真实 ID {sample['news_id']}  ·  模拟窗口 {sample['event_time'][:16].replace('T', ' ')}（北京时间）", 49, 649, 10.8, MUTED)
    metrics = (
        ("曝光", "impressions"), ("点击", "clicks"), ("点击人数", "unique_users"),
        ("访问 PV", "detail_page_views"), ("访问 UV", "detail_visitors"),
    )
    for i, (label, key) in enumerate(metrics):
        x = 49 + i*144
        d.text(label, x, 681, 11.5, MUTED)
        d.text(f"{sample[key]:,}", x, 703, 22, INK, True)
    ratio = "无定义" if not sample["impressions"] else f"{Decimal(sample['clicks']) / Decimal(sample['impressions']) * 100:.2f}%"
    d.text(f"CTR = 点击 / 曝光 = {ratio}；访问 UV 是去重人数，不能把渠道或小时 UV 相加。", 49, 739, 10.5, BLUE)

    d.box(813, 559, 344, 201, PALE_AMBER, AMBER)
    d.text("查询前的三道门", 832, 574, 17, AMBER, True)
    d.para("① 最新修订 + 水位完整 + 质量通过\n② 发布状态可用 + 无屏蔽墓碑\n③ 租户 / 版本 / 数据摘要一致", 832, 613, 306, 12.5, 72)
    d.para("撤稿与清理只在隔离租户演练；\n清理任务全部 pending，未删除实际向量。\n真实撤稿通知与索引执行器留待后续接入。", 832, 699, 306, 10.8, 52, AMBER)

    d.text(f"窗口 {manifest['historical_window_start'][:16]} → {manifest['historical_window_end'][:16]}  |  流式摘要校验 + PostgreSQL 16 导入与对账通过", 32, 779, 10, MUTED)
    d.text("原文 news.qq.com/rain/a/20260903A05YQE00  |  Dataset " + manifest["dataset_sha256"][:20] + "…  |  单页矢量图，可放大查看", 32, 802, 9.5, MUTED)
    d.c.linkURL(sample["url"], (32, 12, 335, 35), relative=0)
    d.c.showPage()
    d.c.save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    render(args.dataset, args.output)
    print(args.output.absolute())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
