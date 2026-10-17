"""Render a single-page Chinese diagram from verified bulk-import receipts."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

CN_FONT = Path("/System/Library/Fonts/STHeiti Medium.ttc")
NAVY = colors.HexColor("#10243A")
TEAL = colors.HexColor("#087F8C")
MINT = colors.HexColor("#E5F6F2")
PAPER = colors.HexColor("#F5F8FA")
GRAY = colors.HexColor("#526477")
LIGHT = colors.HexColor("#C9D6E1")
ORANGE = colors.HexColor("#C56B13")


def render(manifest_path: Path, verification_path: Path, output: Path, *, tests: int) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    if verification.get("status") != "completed":
        raise ValueError("a completed verification receipt is required")
    counts = manifest["counts_by_date"]
    target = manifest["target_per_day"]
    total = sum(counts.values())
    if verification["database_documents"] != total or verification["index_articles"] != total:
        raise ValueError("database and index counts must match the frozen collection")
    original = {"2026-10-07": 19, "2026-10-08": 28, "2026-10-09": 30, "2026-10-10": 26,
                "2026-10-11": 78, "2026-10-12": 120, "2026-10-13": 120, "2026-10-14": 120}
    pdfmetrics.registerFont(TTFont("CN", str(CN_FONT), subfontIndex=0))
    output.parent.mkdir(parents=True, exist_ok=True)
    w, h = landscape(A4)
    pdf = canvas.Canvas(str(output), pagesize=(w, h))
    pdf.setTitle("NewsAgent 每日千篇新闻导入扩容图解")
    pdf.setAuthor("NewsAgent")

    def text(x, top, content, size=10, color=NAVY):
        pdf.setFont("CN", size)
        pdf.setFillColor(color)
        pdf.drawString(x, h - top, str(content))

    def box(x, top, width, height, fill, stroke=None, radius=8):
        pdf.setFillColor(fill)
        pdf.setStrokeColor(stroke or fill)
        pdf.roundRect(x, h - top - height, width, height, radius, fill=1, stroke=int(stroke is not None))

    def arrow(x1, top1, x2, top2, color=TEAL):
        pdf.setStrokeColor(color)
        pdf.setFillColor(color)
        pdf.setLineWidth(1.5)
        pdf.line(x1, h - top1, x2, h - top2)
        path = pdf.beginPath()
        path.moveTo(x2, h - top2)
        path.lineTo(x2 - 6, h - top2 + 3.5)
        path.lineTo(x2 - 6, h - top2 - 3.5)
        path.close()
        pdf.drawPath(path, fill=1, stroke=0)

    pdf.setFillColor(PAPER)
    pdf.rect(0, 0, w, h, fill=1, stroke=0)
    box(0, 0, w, 84, NAVY, radius=0)
    text(30, 33, "新闻导入扩容：每天千篇，逐篇核验", 24, colors.white)
    text(31, 61, f"{manifest['start_date']} 至 {manifest['end_date']} · 北京时间 · 完整正文", 11, colors.HexColor("#BED7DF"))

    cards = [("已采集并入库", f"{total:,} 篇"), ("每日实际目标", f"{target:,} 篇"),
             ("已进入检索索引", f"{verification['index_articles']:,} 篇"),
             ("逐日缺口", f"{sum(max(0, target-n) for n in counts.values()):,} 篇")]
    for i, (label, value) in enumerate(cards):
        x = 30 + i * 198
        box(x, 99, 185, 64, colors.white)
        text(x + 13, 121, label, 10, GRAY)
        text(x + 13, 148, value, 23, TEAL if i != 3 or all(n >= target for n in counts.values()) else ORANGE)

    box(30, 178, 500, 181, colors.white)
    text(44, 200, "每天的完整新闻数量", 13)
    text(44, 218, "灰色：上次导入    绿色：扩容后总量", 9, GRAY)
    chart_top, chart_bottom = 238, 330
    chart_height = chart_bottom - chart_top
    max_value = max(target, max(counts.values()))
    pdf.setDash(3, 3)
    pdf.setStrokeColor(colors.HexColor("#91BFC2"))
    pdf.line(54, h - chart_top, 513, h - chart_top)
    pdf.setDash()
    for i, (day, n) in enumerate(sorted(counts.items())):
        x = 58 + i * 57
        old_height = chart_height * original.get(day, 0) / max_value
        new_height = chart_height * n / max_value
        box(x, chart_bottom - old_height, 12, max(1, old_height), LIGHT, radius=1)
        box(x + 15, chart_bottom - new_height, 24, new_height, TEAL, radius=2)
        text(x + 13, chart_bottom - new_height - 5, str(n), 9, TEAL)
        text(x + 4, 348, day[5:].replace("-", "/"), 9, GRAY)

    box(545, 178, 267, 181, MINT)
    text(560, 202, "导入数量如何算？", 13, TEAL)
    for j, line in enumerate(("以正文 HTML 的发布时间为准", "按 news_id、标题与正文指纹去重", "只有可读完整正文才计入成功", "网址候选数不等于导入数", "原有 541 篇保留并计入每日总量")):
        text(560, 229 + j * 23, "· " + line, 10)

    text(31, 386, "完整调用链", 13)
    nodes = [("公开发现", "站点地图 / 原始响应摘要"), ("正文校验", "清洗、核时间、去重、日配额"),
             ("知识入库", "知识 API / PostgreSQL"), ("向量与索引", "本地 BGE / 1024 维 / HNSW")]
    for i, (title, subtitle) in enumerate(nodes):
        x = 30 + i * 198
        box(x, 402, 185, 64, colors.white, LIGHT)
        text(x + 12, 426, title, 13, TEAL)
        text(x + 12, 449, subtitle, 8.8, GRAY)
        if i < 3:
            arrow(x + 187, 434, x + 196, 434)
    text(31, 491, "下游：API + 热点 Worker 加载同一版本 → 聊天与关联新闻检索", 12)
    text(31, 514, f"核验：数据库内容指纹一致 · 索引全文与元数据一致 · {len(counts)} 天聊天抽验通过 · {tests} 项测试通过", 10, TEAL)
    text(31, 540, "边界：有限公开采集，不代表全量档案；真实热度仍需企业聚合指标和基线。", 10, ORANGE)
    text(31, 569, "配置：每日目标 1000 / 8 并发 / 有界重试 / 分批检查点 / 新版本索引", 9, GRAY)
    text(678, 569, datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d") + "   1 / 1", 9, GRAY)
    pdf.showPage()
    pdf.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--verification", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tests", required=True, type=int)
    args = parser.parse_args()
    render(args.manifest, args.verification, args.output, tests=args.tests)


if __name__ == "__main__":
    main()
