"""Render a verified, diagram-led Chinese guide to the v7 warehouse architecture.

Inputs are the existing v7 artifact manifest, validation record and aggregate
JSONL files. This authoring tool has no database, model or vector-store access.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
from hashlib import sha256
from html import escape
import json
import math
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "output/warehouse/tencent-news-v7-20261014"
OUTPUT = ROOT / "output/pdf/NewsAgent_腾讯新闻模拟数仓架构图解_v7.pdf"
W, H = landscape(A4)
M, CW, TOTAL = 36, W - 72, 7
NAVY = HexColor("#172F4A")
INK = HexColor("#28435C")
GRAY = HexColor("#61758A")
LINE = HexColor("#D8E2ED")
PALE = HexColor("#F3F6FA")
BLUE = HexColor("#2876C8")
GREEN = HexColor("#16876E")
AMBER = HexColor("#A96B17")
RED = HexColor("#B64E54")
PURPLE = HexColor("#7860B0")
BLUE_BG = HexColor("#EDF4FF")
GREEN_BG = HexColor("#EBF6F2")
AMBER_BG = HexColor("#FFF5E4")
RED_BG = HexColor("#FCEEF0")


def file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_evidence(dataset: Path) -> dict:
    """Reject mismatched artifacts instead of publishing invented example values."""
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    validation = json.loads((dataset / "validation.json").read_text(encoding="utf-8"))
    assert manifest["config"] == {"news_per_tenant": 120, "days": 7, "start_date": "2026-10-07", "seed": 20261014}, "this guide illustrates the frozen default v7 dataset"
    assert manifest["synthetic"] is True and manifest["schema"] == "news_sim_v7"
    body = {key: value for key, value in manifest.items() if key != "dataset_sha256"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert sha256(encoded).hexdigest() == manifest["dataset_sha256"], "manifest identity mismatch"
    assert validation["dataset_sha256"] == manifest["dataset_sha256"]
    assert file_hash(dataset / "generator.py") == manifest["generator_sha256"]
    assert file_hash(dataset / "schema.sql") == manifest["schema_sha256"]
    assert file_hash(dataset / "warehouse.sql") == manifest["warehouse_sql_sha256"]
    for table, identity in manifest["tables"].items():
        path = dataset / f"{table}.jsonl"
        assert file_hash(path) == identity["sha256"], f"table fingerprint changed: {table}"
        with path.open("rb") as stream:
            assert sum(1 for _ in stream) == identity["rows"], f"table row count changed: {table}"
    tenant = manifest["tenants"][0]
    news_id = "sim-tn-v7-00001"
    last_hour = "2026-10-13T23:00:00+08:00"
    sample = {}
    for table in ("dim_news", "news_metric_hourly", "news_metric_channel_hourly", "news_metric_5min"):
        rows = []
        with (dataset / f"{table}.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row["tenant_id"] != tenant or row["news_id"] != news_id:
                    continue
                if table != "dim_news" and (not row["event_time"].startswith("2026-10-13T23:") or row["snapshot_revision"] != 2):
                    continue
                rows.append(row)
        sample[table] = rows
    assert len(sample["dim_news"]) == 1 and len(sample["news_metric_hourly"]) == 1
    assert len(sample["news_metric_channel_hourly"]) == 3 and len(sample["news_metric_5min"]) == 36
    hourly = sample["news_metric_hourly"][0]
    channels = sample["news_metric_channel_hourly"]
    assert sum(row["clicks"] for row in channels) == hourly["clicks"]
    assert max(row["unique_users"] for row in channels) <= hourly["unique_users"] < sum(row["unique_users"] for row in channels)
    return {"manifest": manifest, "validation": validation, "sample": sample, "sample_hour": last_hour}


class WarehouseArchitectureGuide:
    def __init__(self, output: Path, evidence: dict, font: Path, bold_font: Path):
        pdfmetrics.registerFont(TTFont("CN", str(font), subfontIndex=0))
        pdfmetrics.registerFont(TTFont("CN-Bold", str(bold_font), subfontIndex=0))
        self.c = canvas.Canvas(str(output), pagesize=(W, H), pageCompression=1)
        self.c.setTitle("NewsAgent 腾讯新闻模拟数仓架构图解 v7")
        self.c.setAuthor("NewsAgent")
        self.c.setSubject("三条数据线路、13表、指标聚合、水位与修订、Agent、撤稿向量清理和实现边界")
        self.e, self.page = evidence, 0

    def rect(self, x, y, width, height, fill=PALE, stroke=None, dashed=False, radius=10):
        assert x >= M - 1 and x + width <= W - M + 1 and y + height <= H - 43, (self.page, x, y, width, height)
        self.c.saveState()
        self.c.setFillColor(fill)
        self.c.setStrokeColor(stroke or fill)
        self.c.setLineWidth(1)
        if dashed:
            self.c.setDash(4, 3)
        self.c.roundRect(x, H - y - height, width, height, radius, fill=1, stroke=bool(stroke))
        self.c.restoreState()

    def text(self, value, x, y, size=11, color=INK, bold=False, width=None):
        value, font = str(value), "CN-Bold" if bold else "CN"
        actual = pdfmetrics.stringWidth(value, font, size)
        limit = width if width is not None else W - M - x
        if actual > limit + .5:
            raise ValueError(f"page {self.page}: text exceeds {limit}: {value}")
        assert y + size < H - 15
        self.c.setFillColor(color)
        self.c.setFont(font, size)
        self.c.drawString(x, H - y - size * .85, value)

    def para(self, value, x, y, width, size=10.5, color=INK, maxh=60, bold=False, leading=None):
        style = ParagraphStyle("p", fontName="CN-Bold" if bold else "CN", fontSize=size,
                               leading=leading or size * 1.5, textColor=color, wordWrap="CJK")
        paragraph = Paragraph(escape(str(value)).replace("\n", "<br/>"), style)
        _, height = paragraph.wrap(width, H)
        if height > maxh + .5 or y + height > H - 43:
            raise ValueError(f"page {self.page}: paragraph overflow {height}/{maxh}: {value}")
        paragraph.drawOn(self.c, x, H - y - height)
        return height

    def line(self, x1, y1, x2, y2, color=LINE, width=1, dashed=False):
        self.c.saveState()
        self.c.setStrokeColor(color)
        self.c.setLineWidth(width)
        if dashed:
            self.c.setDash(4, 3)
        self.c.line(x1, H - y1, x2, H - y2)
        self.c.restoreState()

    def arrow(self, x1, y1, x2, y2, color=BLUE, dashed=False):
        self.line(x1, y1, x2, y2, color, 1.5, dashed)
        angle = math.atan2(y2 - y1, x2 - x1)
        left = (x2 - 7 * math.cos(angle) + 3 * math.sin(angle), y2 - 7 * math.sin(angle) - 3 * math.cos(angle))
        right = (x2 - 7 * math.cos(angle) - 3 * math.sin(angle), y2 - 7 * math.sin(angle) + 3 * math.cos(angle))
        p = self.c.beginPath()
        p.moveTo(x2, H - y2)
        p.lineTo(left[0], H - left[1])
        p.lineTo(right[0], H - right[1])
        p.close()
        self.c.setFillColor(color)
        self.c.drawPath(p, fill=1, stroke=0)

    def tag(self, value, x, y, color=GREEN, fill=GREEN_BG, width=None):
        width = width or pdfmetrics.stringWidth(value, "CN", 9) + 18
        self.rect(x, y, width, 22, fill, radius=5)
        self.text(value, x + 9, y + 6, 9, color, width=width - 18)

    def card(self, x, y, width, height, title, body, color=BLUE, fill=PALE, dashed=False, size=10.5):
        self.rect(x, y, width, height, fill, color if dashed else None, dashed)
        self.text(title, x + 14, y + 14, 13, color, True, width=width - 28)
        self.para(body, x + 14, y + 39, width - 28, size, maxh=height - 47)

    def note(self, title, body, y, color=BLUE, fill=BLUE_BG, height=65):
        self.rect(M, y, CW, height, fill)
        self.text(title, M + 15, y + 12, 12, color, True)
        self.para(body, M + 15, y + 33, CW - 30, 10.5, maxh=height - 36)

    def start(self, section, title, subtitle):
        self.page += 1
        self.c.bookmarkPage(f"page{self.page}")
        self.c.addOutlineEntry(title, f"page{self.page}", level=0)
        self.text(f"NEWSAGENT / {section}", M, 24, 9, BLUE, True)
        self.text(title, M, 49, 25, NAVY, True)
        self.para(subtitle, M, 88, CW, 11, GRAY, maxh=33)

    def end(self):
        self.line(M, H - 35, W - M, H - 35, LINE, .7)
        self.text("腾讯新闻业务背景模拟 | 全部运营数字为合成值 | 2026-10-14", M, H - 25, 8, GRAY)
        self.text(f"{self.page:02d} / {TOTAL:02d}", W - M - 39, H - 25, 8, GRAY)
        self.c.showPage()

    def overview(self):
        self.start("01 / THE BIG PICTURE", "把数仓想成新闻运营的“物流枢纽”",
                   "行为数据送来账目，内容系统送来正文，生命周期送来通行规则；Agent 在下游组合这些证据。")
        self.tag("绿色：本地已有", M, 128)
        self.tag("虚线：目标接线 / 待实现", M + 129, 128, AMBER, AMBER_BG)
        self.tag("腾讯新闻内部技术栈待确认", M + 312, 128, GRAY, PALE)
        x1, x2, x3 = M, M + 248, M + 514
        for y, label, desc, center, centerdesc, color in (
            (169, "行为线路", "曝光、点击、消费、互动\n上游企业平台负责原始采集", "运营数仓 = 账本", "按时间、新闻、渠道保存聚合\n回答：谁受关注，变化多大？", BLUE),
            (278, "内容线路", "新闻正文、来源、发布时间\n图文、视频文本化、QA", "知识库 = 图书馆", "文档与切片是事实台账\n向量是可重建的检索目录", PURPLE),
            (387, "状态线路", "发布、更正、撤稿、受限\n可信内容状态 + 版本事件", "资格门禁 = 通行闸门", "当前状态、墓碑、清理任务\n决定：此刻还能否使用？", AMBER),
        ):
            self.card(x1, y, 218, 87, label, desc, color, size=10)
            self.arrow(x1 + 221, y + 43, x2 - 5, y + 43, color, dashed=True)
            self.card(x2, y, 233, 87, center, centerdesc, color, size=10)
        self.card(x3, 209, 255, 219, "NewsAgent = 运营工作台",
                  "受限 SQL 读取权威指标\n+ 正文与关联证据\n+ 当前内容资格\n\nPython 计算排行和校验\nLLM 解释、总结、辅助写作", NAVY, BLUE_BG, size=11)
        for y, color in ((212, BLUE), (321, PURPLE), (430, AMBER)):
            self.arrow(x2 + 236, y, x3 - 5, y if y < 400 else 418, color, dashed=True)
        self.note("当前落点", "v7 已生成隔离 PostgreSQL 数据包和资格视图；既有 v6 热点链路独立于本包。v7 到 Agent、正文和向量库的连线尚未接入。", 492, GREEN, GREEN_BG, 58)
        self.end()

    def table_map(self):
        self.start("02 / THE TABLE MAP", "13 张表：给谁记账、记什么、何时可用",
                   "每张表先看“一行是什么”。共同关联键是 tenant_id + news_id；表数多，是因为职责不同。")
        colw, gap = (CW - 24) / 3, 12
        groups = (
            (M, 137, colw, 170, "A  身份证与字典 · 3 表", GREEN,
             [("dim_news", "一租户一新闻：题材、形态、版本、状态"),
              ("dim_publisher", "一租户一来源：媒体、创作者等合成类型"),
              ("dim_channel", "一租户一渠道：推荐、搜索、推送等")]),
            (M + colw + gap, 137, colw, 170, "B  运营账本 · 3 表", BLUE,
             [("news_metric_5min", "新闻 × 渠道 × 五分钟 × 修订"),
              ("news_metric_channel_hourly", "新闻 × 渠道 × 小时 × 修订"),
              ("news_metric_hourly", "新闻 × 小时 × 修订；跨渠道 UV")]),
            (M + 2 * (colw + gap), 137, colw, 170, "C  通行与清理 · 3 表", AMBER,
             [("content_change_event", "可信变更事件：更正、撤稿、受限"),
              ("content_tombstone", "撤稿 / 受限内容的持久屏蔽记录"),
              ("index_cleanup_task", "分目标、分版本的待清理任务")]),
            (M, 321, colw, 135, "D  质检与参照 · 2 表", PURPLE,
             [("window_snapshot", "小时 × 修订：水位、完整性、版本"),
              ("news_metric_baseline_hourly", "同新闻前 1-3 天相同小时的均值")]),
            (M + colw + gap, 321, colw, 135, "E  原榜快照 · 2 表", NAVY,
             [("board_snapshot", "榜单 × 快照：时间与规则版本"),
              ("board_item", "原始名次、位置、分数；保留并列")]),
        )
        for x, y, width, height, title, color, rows in groups:
            self.rect(x, y, width, height)
            self.text(title, x + 12, y + 12, 12.5, color, True, width=width - 24)
            for index, (name, desc) in enumerate(rows):
                yy = y + 39 + index * 41
                self.text(name, x + 12, yy, 9.6, INK, True, width=width - 24)
                self.para(desc, x + 12, yy + 16, width - 24, 9.5, GRAY, maxh=24)
        self.card(M + 2 * (colw + gap), 321, colw, 135, "视图 = 服务柜台",
                  "最新修订 + 完整窗口\n+ 当前可用内容 + 空基线保留\n→ 18 列兼容查询投影", GREEN, GREEN_BG, size=11)
        self.note("记住四个约束", "租户隔离；计数关系校验；修订号绑定；内容资格过滤。视图只是投影，历史账本与当前可用结果可以不同。", 475, BLUE, BLUE_BG, 66)
        self.end()

    def aggregation(self):
        sample = self.e["sample"]
        hourly = sample["news_metric_hourly"][0]
        channels = sample["news_metric_channel_hourly"]
        self.start("03 / ONE NEWS ITEM", "一篇新闻：点击可以相加，人数要去重",
                   "下面直接取 v7 数据包的合成样例：租户 A、案例 001、2026-10-13 23:00-24:00、修订 2。")
        widths = (CW - 24) / 3
        labels = {"recommend": "推荐信息流", "search": "搜索结果", "push": "消息推送入口"}
        for index, row in enumerate(channels):
            x = M + index * (widths + 12)
            self.rect(x, 137, widths, 113, PALE)
            self.text(labels[row["channel_id"]], x + 14, 151, 13, BLUE, True)
            self.text(f"{row['clicks']:,} 次点击", x + 14, 179, 21, NAVY, True)
            self.text(f"{row['unique_users']:,} 人 / 渠道小时 UV", x + 14, 213, 11, GRAY)
            self.arrow(x + widths / 2, 254, x + widths / 2, 280, BLUE)
        self.rect(M, 283, CW, 74, BLUE_BG)
        clicks = " + ".join(str(row["clicks"]) for row in channels)
        uvs = " + ".join(str(row["unique_users"]) for row in channels)
        self.text(f"次数账：{clicks} = {hourly['clicks']:,} 次点击", M + 16, 298, 17, BLUE, True)
        self.text(f"人数账：{uvs} = {sum(row['unique_users'] for row in channels)}，但去重后是 {hourly['unique_users']} 人", M + 16, 327, 14, GREEN, True)
        self.rect(M, 372, 373, 103, GREEN_BG)
        self.text("为什么少了？同一个人会跨渠道重复消费", M + 13, 385, 12, GREEN, True)
        self.para("UV 保存并集结果。v7 用临时合成区间模拟重叠，只落聚合人数；小时 UV 也不能相加后称为全天去重人数。", M + 13, 410, 347, 11, maxh=54)
        self.rect(M + 388, 372, CW - 388, 103, PALE)
        ctr = (Decimal(hourly["clicks"]) / hourly["impressions"] * 100).quantize(Decimal("0.01"))
        self.text("比例必须由总量再计算", M + 401, 385, 12, NAVY, True)
        self.text(f"CTR = {hourly['clicks']:,} / {hourly['impressions']:,} = {ctr}%", M + 401, 412, 14, BLUE, True)
        self.para("不直接平均各桶 CTR；零分母保留 NULL。\n有效消费、时长和互动均由 Python / SQL 算。", M + 401, 438, CW - 415, 9.7, GRAY, maxh=32)
        self.note("桶怎么变成小时？", "每渠道 12 个五分钟桶 → 渠道小时聚合 → 新闻小时聚合。当前细桶文件仅保留最后闭合小时两次修订和开放小时。", 491, AMBER, AMBER_BG, 57)
        self.end()

    def time_windows(self):
        self.start("04 / TIME & QUALITY", "到整点，不等于这一小时已经完整",
                   "把窗口看成一只“封箱快递”：event_time 是货物归属时间，snapshot_at 是盘点时间，水位和质量决定能否交付。")
        self.line(M + 15, 159, W - M - 15, 159, BLUE, 3)
        ticks = ((M + 15, "23:00"), (M + 320, "23:45"), (M + 493, "00:00"), (W - M - 15, "00:35"))
        for x, label in ticks:
            self.line(x, 152, x, 168, BLUE, 2)
            self.text(label, min(x, W - M - 42), 174, 10, BLUE)
        self.text("闭合小时 [23:00, 00:00)", M + 65, 133, 12, NAVY, True)
        self.text("开放小时 [00:00, 01:00)", M + 525, 133, 12, AMBER, True)
        width = (CW - 24) / 3
        cases = (
            ("第一次盘点 · rev1", "最后闭合小时\n水位 23:45 / 快照 23:50\nincomplete + pending\n还不能进入完整小时榜", AMBER, AMBER_BG),
            ("补数后的盘点 · rev2", "同一闭合小时\n水位 00:10 / 快照 00:15\ncorrected + passed\n最新且完整，才可进入榜单", GREEN, GREEN_BG),
            ("进行中的新小时", "开放小时 rev1\n水位 00:30 / 快照 00:35\n只生成 7 个五分钟桶\n仍然 incomplete + pending", AMBER, AMBER_BG),
        )
        for i, (title, body, color, fill) in enumerate(cases):
            self.card(M + i * (width + 12), 210, width, 144, title, body, color, fill, size=10.8)
        for x1, x2 in ((M + width + 3, M + width + 9), (M + 2 * width + 15, M + 2 * width + 21)):
            self.arrow(x1, 282, x2, 282, GRAY)
        self.text("查询的顺序", M, 379, 14, NAVY, True)
        stages = (("1  先挑最新修订", "固定租户 + 小时 + revision"),
                  ("2  再验完整与质量", "complete / corrected 且 passed"),
                  ("3  才输出可用结果", "不回退旧修订冒充本次数据"))
        for index, (title, body) in enumerate(stages):
            xx = M + index * (width + 12)
            self.card(xx, 406, width, 73, title, body, BLUE, size=10.2)
        self.note("水位要配合合同使用", "它表达事件时间进度；还需迟到预算、来源覆盖和质量校验。当前 v7 离线模拟这些状态，持续流消费和实时服务仍待实现。", 491, BLUE, BLUE_BG, 57)
        self.end()

    def agent_and_knowledge(self):
        self.start("05 / AGENT & KNOWLEDGE", "数字、正文、向量：各自回答不同问题",
                   "数仓回答“多少、何时、怎么变”；正文回答“发生了什么”；向量帮你找到有关材料。三者由新闻身份连接。")
        width = (CW - 24) / 3
        descriptions = (
            ("运营数仓 · 账本", "曝光、点击、UV、时长、互动\n基线、窗口、水位与数据版本\n权威数字来自 Python / SQL", BLUE),
            ("文档台账 · 图书馆", "正文、来源、文档和切片版本\n必要证据与派生血缘\n向量删除不替代文档状态治理", PURPLE),
            ("向量索引 · 检索目录", "Embedding → 相关材料候选\nMilvus / BGE 等检索 Adapter\n相似度不能作为新闻热度", GREEN),
        )
        for i, (title, body, color) in enumerate(descriptions):
            self.card(M + i * (width + 12), 137, width, 125, title, body, color, size=10.5)
        self.rect(M, 277, CW, 34, NAVY)
        self.text("统一身份：tenant_id + news_id；正文版本、生命周期修订、数据版本分别保留", M + 14, 288, 12, white, True)
        self.text("现有热点运行：下面的流程已经有本地链路", M, 330, 13, GREEN, True)
        stages = (("提问 / 调度", "身份与有界窗口"), ("受限 SQL", "意图 → 编译 → 校验"),
                  ("Python 指标", "候选 → 热度排行"), ("正文与证据", "精确新闻 + 关联召回"),
                  ("LLM / 校验", "分析表达 → 保存报告"))
        bw, gap = (CW - 40) / 5, 10
        for i, (title, desc) in enumerate(stages):
            xx = M + i * (bw + gap)
            self.rect(xx, 359, bw, 75, GREEN_BG)
            self.text(title, xx + 11, 372, 12, GREEN, True, width=bw - 22)
            self.para(desc, xx + 11, 398, bw - 22, 9.7, maxh=30)
            if i < 4:
                self.arrow(xx + bw + 1, 396, xx + bw + gap - 1, 396, GREEN)
        self.card(M, 450, 244, 85, "v7 隔离数据包", "新 schema / 13 表 / 5 视图\n已导入验证；包含合成标题，无正文或向量", BLUE, BLUE_BG, size=10)
        self.arrow(M + 248, 491, M + 290, 491, AMBER, dashed=True)
        self.card(M + 297, 450, CW - 297, 85, "待接入上面的共享运行", "注册新契约和白名单，绑定窗口修订及质量证据，建立正文 / chunk 映射，并复用现有热点 Workflow。", AMBER, AMBER_BG, True, size=10.5)
        self.end()

    def withdrawal(self):
        self.start("06 / WITHDRAWAL", "撤稿像关闭通行闸门，再清理各处副本",
                   "先让内容不可再使用；向量删除可以异步执行，删除失败也不应该恢复可见性。")
        self.tag("v7 已有：事件、墓碑、SQL 资格过滤、pending 任务", M, 128)
        self.tag("真实读链门禁 / 删除 Worker 待接", M + 372, 128, AMBER, AMBER_BG)
        steps = (("1  收到可信撤稿", "租户 + 新闻 + 状态修订", AMBER),
                 ("2  提交状态与墓碑", "立即阻止新证据使用", RED),
                 ("3  异步清理投影", "版本映射、重试、审计", BLUE),
                 ("4  验证无被禁版本", "验证后才记为完成", GREEN))
        bw, gap = (CW - 36) / 4, 12
        for i, (title, body, color) in enumerate(steps):
            x = M + i * (bw + gap)
            self.card(x, 165, bw, 81, title, body, color, dashed=True, size=10)
            if i < 3:
                self.arrow(x + bw + 1, 205, x + bw + gap - 1, 205, AMBER, dashed=True)
        self.text("同一新闻，在三种存储的处理方式", M, 263, 13, NAVY, True)
        width = (CW - 24) / 3
        targets = (
            ("PostgreSQL 知识台账", "停用 / 清理文档与切片\n先建立新闻到 chunk 的版本映射\n当前缺撤稿状态入口", PURPLE),
            ("Milvus 向量索引", "按已核对的旧版本 chunk 删除\n不能只凭 news_id 跨租户删除\n删除后再检查被禁版本残留", BLUE),
            ("BGE / HNSW 只读快照", "先在读取侧应用屏蔽名单\n再重建并切换新快照\n旧快照恢复仍要加载墓碑", GREEN),
        )
        for i, (title, body, color) in enumerate(targets):
            self.card(M + i * (width + 12), 292, width, 114, title, body, color, size=10.5)
        self.rect(M, 421, CW, 64, RED_BG)
        self.text("防止“删了又回来”：正文版本和状态修订分开", M + 14, 433, 12, RED, True)
        self.para("撤稿可以是 content_version=1、lifecycle_revision=2。晚到入库不能自行复刊；旧清理任务只能删绑定的旧版本，不能误删合法新版。", M + 14, 457, CW - 28, 10.2, maxh=27)
        self.note("还要处理副本", "缓存、QA、视频转写、报告、会话摘要和备份都要有来源血缘与失效规则。现有检索仍缺统一最终状态检查，v7 任务尚未执行真实删除。", 496, AMBER, AMBER_BG, 52)
        self.end()

    def status_and_references(self):
        manifest, validation = self.e["manifest"], self.e["validation"]
        total_rows = sum(table["rows"] for table in manifest["tables"].values())
        self.start("07 / WHAT EXISTS TODAY", "现在在哪里，下一步接什么",
                   "本册依据当前代码、版本化数据包和保存的验收记录绘制。公开腾讯新闻定位用于业务背景，内部表名与技术栈仍待确认。")
        stats = (("240", "合成新闻 / 两租户"), (f"{total_rows:,}", "13 表总记录数"),
                 ("33,600", "完整且可用的指标投影"), (str(validation["new_unit_contract_tests"]["passed"]), "专项测试记录通过"))
        sw, gap = (CW - 36) / 4, 12
        for i, (number, label) in enumerate(stats):
            x = M + i * (sw + gap)
            self.rect(x, 133, sw, 74, GREEN_BG)
            self.text(number, x + 13, 145, 22, GREEN, True)
            self.text(label, x + 13, 179, 9.7, INK, width=sw - 26)
        self.card(M, 221, 373, 105, "已有：可以复查的本地数据包",
                  "6 渠道、4 形态、12 主题、17 类合成模式\n计数 / UV / 基线 / 修订 / 生命周期台账\n隔离 PG16 导入、只读核对及重复导入拒绝", GREEN, GREEN_BG, size=10.8)
        self.card(M + 387, 221, CW - 387, 105, "待接：运行与治理闭环",
                  "v7 → Agent 的新契约 / 白名单 / 质量 Port\n可信状态门禁 → Outbox / Worker → 真实清理\n持续流、企业口径、权限与生产验收", AMBER, AMBER_BG, True, size=10.8)
        self.text("这份图解如何生成", M, 341, 13, NAVY, True)
        self.para("manifest / validation / 聚合 JSONL → load_evidence() 校验文件哈希和样例 → WarehouseArchitectureGuide → ReportLab + 中文字体 → 7 页可缩放 PDF。", M, 366, CW, 10.5, maxh=35)
        self.para("配置：--dataset / --output / --font / --bold-font。外部依赖：ReportLab；核验使用 pypdf、pdfplumber、Poppler。测试入口：运行本脚本完成源文件校验，再逐页渲染检查；不调用企业模型或向量库。", M, 407, CW, 9.6, GRAY, maxh=31)
        self.text("依据与延伸阅读（点击可打开）", M, 451, 11, NAVY, True)
        references = (
            ("腾讯新闻公开产品定位", "https://www.tencent.com/zh-cn/products/tencent-news/"),
            ("腾讯云事件时间 / Watermark 通用语义", "https://cloud.tencent.com/document/product/849/18034"),
            ("项目 v7 设计与验收", "agent/docs/tencent-news-simulation-v7.md；output/warehouse/tencent-news-v7-20261014/validation.json"),
        )
        for index, (title, target) in enumerate(references):
            y = 473 + index * 20
            self.text(f"{index + 1}. {title}", M, y, 9.5, INK)
            self.text(target, M + 234, y, 8.5, BLUE, width=CW - 234)
            if target.startswith("https://"):
                self.c.linkURL(target, (M + 230, H - y - 13, W - M, H - y + 1), relative=0, thickness=0)
        self.end()

    def render(self):
        for method in (self.overview, self.table_map, self.aggregation, self.time_windows,
                       self.agent_and_knowledge, self.withdrawal, self.status_and_references):
            method()
        assert self.page == TOTAL
        self.c.save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--font", type=Path, default=Path("/System/Library/Fonts/STHeiti Light.ttc"))
    parser.add_argument("--bold-font", type=Path, default=Path("/System/Library/Fonts/STHeiti Medium.ttc"))
    args = parser.parse_args()
    evidence = load_evidence(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    WarehouseArchitectureGuide(args.output, evidence, args.font, args.bold_font).render()
    print(json.dumps({"output": str(args.output.resolve()), "pages": TOTAL,
                      "sha256": file_hash(args.output), "source_dataset_sha256": evidence["manifest"]["dataset_sha256"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
