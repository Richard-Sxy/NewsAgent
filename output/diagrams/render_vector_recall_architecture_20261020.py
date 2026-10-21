"""Create a one-page Chinese architecture diagram from the existing v2 assets.

Upstream: v1/v2 build receipts, v2 chunks, and the v2 retrieval YAML.
Transform: validate article/chunk totals and compute explicitly dated demo tiers.
Core: collect_facts -> render_architecture -> ReportLab PDF with embedded fonts.
Config: --output; snapshot paths; demo date 2026-10-15 and <=1/<=4 day bounds.
QA: pypdf one-page/text checks, then Poppler rendering and visual inspection.
TODO: real three-tier index construction and a separately measured replay.
This renderer never loads a model, calls enterprise services, or changes indexes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
import math
from pathlib import Path
import re

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "var/agent/data"
V2 = PACKAGE_ROOT / "news-bge-incremental-20261014-v2"
W, H = landscape(A3)
INK = HexColor("#183747")
MUTED = HexColor("#526B7A")
RULE = HexColor("#D7E1E6")
GREEN = HexColor("#117464")
GREEN_BG = HexColor("#EDF7F3")
BLUE = HexColor("#2B6498")
BLUE_BG = HexColor("#EEF4FA")
AMBER = HexColor("#9E641A")
AMBER_BG = HexColor("#FFF6E8")
GRAY_BG = HexColor("#F3F6F8")


def collect_facts() -> dict:
    reports = {}
    for version in ("v1", "v2"):
        path = PACKAGE_ROOT / f"news-bge-incremental-20261014-{version}/build_report.json"
        reports[version] = json.loads(path.read_text(encoding="utf-8"))
        if reports[version]["status"] != "completed":
            raise ValueError("a completed build receipt is required")
    config_text = (ROOT / "deploy/knowledge-search.incremental-20261014-v2.yml").read_text()
    if re.findall(r"^backend:\s*(\S+)\s*$", config_text, re.MULTILINE) != ["bge_snapshot"]:
        raise ValueError("the diagram describes the configured BGE snapshot adapter")
    # This read-only figure needs only two integer scalars from the fixed config;
    # reject missing/ambiguous values rather than requiring another runtime library.
    search = {}
    for key in ("chunk_pool", "ef"):
        matches = re.findall(rf"^\s+{key}:\s*(\d+)\s*$", config_text, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError(f"expected exactly one integer retrieval setting: {key}")
        search[key] = int(matches[0])
    articles = {}
    article_dates = {}
    chunk_counts = Counter()
    article_counts = Counter()
    now = date(2026, 10, 15)
    for line in (V2 / "data/experiments/offline_recall_fg/chunks.jsonl").open(encoding="utf-8"):
        row = json.loads(line)
        news_id, published = row["news_id"], row.get("publish_time")
        if news_id in article_dates and article_dates[news_id] != published:
            raise ValueError("publication metadata differs within an article")
        article_dates[news_id] = published
        if not published:
            tier = "unknown"
        else:
            age = (now - date.fromisoformat(published[:10])).days
            if age < 0:
                raise ValueError("future content cannot enter this dated demonstration")
            tier = "hot" if age <= 1 else "warm" if age <= 4 else "cold"
        chunk_counts[tier] += 1
        if news_id not in articles:
            articles[news_id] = tier
            article_counts[tier] += 1
    v1, v2 = reports["v1"], reports["v2"]
    assert len(articles) == v2["total_articles"] == 20187
    assert sum(chunk_counts.values()) == v2["total_chunks"] == 50252
    assert v1["total_articles"] + v2["added_articles"] == v2["total_articles"]
    assert v1["total_chunks"] + v2["added_chunks"] == v2["total_chunks"]
    assert dict(article_counts) == {"unknown": 12187, "hot": 1000, "warm": 3000, "cold": 4000}
    assert dict(chunk_counts) == {"unknown": 36379, "hot": 1598, "warm": 5268, "cold": 7007}
    return {"v1": v1, "v2": v2, "search": search,
            "articles": article_counts, "chunks": chunk_counts, "demo_date": now.isoformat()}


class Drawing:
    def __init__(self, output: Path):
        pdfmetrics.registerFont(TTFont("CN", "/System/Library/Fonts/STHeiti Light.ttc", subfontIndex=0))
        pdfmetrics.registerFont(TTFont("CNB", "/System/Library/Fonts/STHeiti Medium.ttc", subfontIndex=0))
        self.c = canvas.Canvas(str(output), pagesize=(W, H), pageCompression=1)
        self.c.setTitle("NewsAgent 向量召回架构与冷温热分层")
        self.c.setAuthor("NewsAgent")
        self.c.setSubject("现有单库HNSW、未构建的日期分层演练、独立在线生命周期；2026-10-20核查")

    def text(self, value, x, y, size=12, color=INK, bold=False, center=False, maxw=None):
        value = str(value)
        font = "CNB" if bold else "CN"
        if maxw and pdfmetrics.stringWidth(value, font, size) > maxw:
            raise ValueError(f"text exceeds available width: {value}")
        self.c.setFillColor(color)
        self.c.setFont(font, size)
        draw = self.c.drawCentredString if center else self.c.drawString
        draw(x, H - y - size, value)

    def box(self, x, y, w, h, fill=white, stroke=RULE, dashed=False, radius=10):
        self.c.saveState()
        self.c.setFillColor(fill)
        self.c.setStrokeColor(stroke)
        self.c.setLineWidth(0.9)
        if dashed:
            self.c.setDash(4, 3)
        self.c.roundRect(x, H - y - h, w, h, radius, stroke=1, fill=1)
        self.c.restoreState()

    def route(self, points, color=GREEN, dashed=False, width=1.4):
        self.c.saveState()
        self.c.setStrokeColor(color)
        self.c.setFillColor(color)
        self.c.setLineWidth(width)
        if dashed:
            self.c.setDash(4, 3)
        path = self.c.beginPath()
        path.moveTo(points[0][0], H - points[0][1])
        for x, y in points[1:]:
            path.lineTo(x, H - y)
        self.c.drawPath(path, stroke=1, fill=0)
        x1, y1 = points[-2]
        x2, y2 = points[-1]
        angle = math.atan2(y2-y1, x2-x1)
        head = self.c.beginPath()
        head.moveTo(x2, H-y2)
        head.lineTo(x2 - 6*math.cos(angle) + 3*math.sin(angle), H-(y2 - 6*math.sin(angle) - 3*math.cos(angle)))
        head.lineTo(x2 - 6*math.cos(angle) - 3*math.sin(angle), H-(y2 - 6*math.sin(angle) + 3*math.cos(angle)))
        head.close()
        self.c.drawPath(head, stroke=0, fill=1)
        self.c.restoreState()

    def title(self, number, title, x, y, color=GREEN):
        self.c.setFillColor(color)
        self.c.circle(x+10, H-y-11, 10, stroke=0, fill=1)
        self.text(number, x+10, y+2, 12, white, True, True)
        self.text(title, x+29, y-1, 16, color, True)

    def node(self, x, y, w, h, title, lines, color=GREEN, fill=GREEN_BG, dashed=False):
        self.box(x, y, w, h, fill, color, dashed)
        self.text(title, x+14, y+12, 14, color, True, maxw=w-28)
        for offset, (text, size, strong) in enumerate(lines):
            self.text(text, x+14, y+39+offset*25, size, INK if strong else MUTED,
                      strong, maxw=w-28)


def render_architecture(facts: dict, output: Path) -> None:
    d = Drawing(output)
    d.c.setFillColor(white)
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    v1, v2, search = facts["v1"], facts["v2"], facts["search"]
    ac, cc = facts["articles"], facts["chunks"]
    d.text("NEWSAGENT / VECTOR RECALL / 2026-10-20", 32, 20, 10, GREEN, True)
    d.text("向量召回架构与冷温热分层", 32, 42, 28, INK, True)
    d.text("现有资产：单库 HNSW；分层演练与在线迁移独立说明。数量单位：新闻篇数 / 向量切片数。", 32, 84, 12, MUTED)
    d.route([(900, 36), (932, 36)], GREEN)
    d.text("现有链路", 940, 28, 10.5, GREEN)
    d.route([(1032, 36), (1064, 36)], AMBER, True)
    d.text("演练分配", 1072, 28, 10.5, AMBER)

    # A. Build the new vectors and merge them with the already frozen v1 rows.
    d.title("1", "向量构建：新增正文编码，合并后重建整张 HNSW 图", 32, 119)
    d.box(827, 117, 330, 44, GRAY_BG, RULE)
    d.text(f"继承 v1：{v1['total_articles']:,} 篇 / {v1['total_chunks']:,} 片", 842, 124, 12, INK, True)
    d.text("旧切片、旧向量保持原版本", 842, 143, 10, MUTED)
    d.route([(992, 161), (992, 176)], GREEN)

    d.node(32, 176, 235, 109, "真实新闻输入", [
        (f"新增 {v2['added_articles']:,} 篇", 20, True),
        ("公开正文 + 标题 + 来源 + 发布时间", 10.8, False),
        ("冻结 manifest / 哈希校验", 10.8, False)])
    d.node(297, 176, 235, 109, "确定性切片", [
        (f"新增 {v2['added_chunks']:,} 片", 20, True),
        ("正文 800 字符 / 重叠 80 字符", 11.5, False),
        ("每片重复标题、来源与时间头", 11, False)])
    d.node(562, 176, 235, 109, "BGE-large-zh-v1.5", [
        (f"{v2['dimensions']:,} 维", 20, True),
        ("L2 归一化 / 存储 float32", 11.5, False),
        ("模型上限 512 token，需关注截断", 10.7, False)])
    d.node(827, 176, 330, 109, "单库 HNSW 索引 / 只读快照", [
        (f"{v2['total_articles']:,} 篇 / {v2['total_chunks']:,} 片", 22, True),
        ("IP 内积 / M=32 / efConstruction=200", 11.5, False),
        ("label = 切片文件行号；HOT 是路由标记", 11, False)])
    for x1, x2 in ((267, 297), (532, 562), (797, 827)):
        d.route([(x1, 231), (x2, 231)], GREEN)
    d.text("新增只计算新新闻的向量；HNSW 使用全部向量重新构建。该快照拒绝写入和删除，不承担在线内容更正。", 32, 297, 11, MUTED)

    # B. Every query uses the encoder that belongs to this frozen vector space.
    d.title("2", "应用召回：当前 BGE 路径只有单一 HOT 索引", 32, 334)
    d.route([(992, 285), (992, 319), (536, 319), (536, 374)], GREEN)
    nodes = [
        (32, 170, "业务入口", [("聊天 search_knowledge", 10.8, False), ("热点关联：标题 + 摘要", 10.8, False)]),
        (229, 170, "查询 Embedding", [("配套 BGE + 查询前缀", 10.6, False), ("1024 维 / 归一化", 11, False)]),
        (426, 220, "HNSW 搜索", [(f"起始候选池 {search['chunk_pool']} 片", 12, True), (f"ef = max({search['ef']}, 当前池大小)", 11, False)]),
        (673, 220, "过滤与排名融合", [("排除源新闻 / 按 news_id 去重", 10.6, False), ("不足则扩池 / RRF 排名分数", 10.8, False)]),
        (920, 237, "业务输出", [("热点：规则重排 → 证据 → 报告", 10.6, False), ("聊天：工具检查点 → SSE / 页面", 10.5, False)]),
    ]
    for x, width, title, lines in nodes:
        d.node(x, 374, width, 88, title, lines, BLUE, BLUE_BG)
    for x1, x2 in ((202, 229), (399, 426), (646, 673), (893, 920)):
        d.route([(x1, 418), (x2, 418)], BLUE)
    d.text("索引工厂选择 bge_snapshot，并绑定同版本查询模型；RRF / 业务重排分数均不代表新闻热度或校准后的相关概率。", 32, 473, 10.7, MUTED)

    # C. Inventory projection only. Dashed boxes do not assert physical indexes.
    d.title("3", "冷温热演练：仅计算分配，三层索引尚未构建", 32, 513, AMBER)
    d.text("冻结观察日 2026-10-15；热 ≤ 1 天，温：1 < 天数 ≤ 4，冷 > 4 天。", 32, 542, 10.6, MUTED)
    known_articles = sum(ac[t] for t in ("hot", "warm", "cold"))
    known_chunks = sum(cc[t] for t in ("hot", "warm", "cold"))
    d.box(220, 567, 316, 35, AMBER_BG, AMBER, True)
    d.text(f"日期明确：{known_articles:,} 篇 / {known_chunks:,} 片", 378, 576, 12.3, AMBER, True, True)
    d.route([(378, 602), (378, 612), (139, 612), (139, 623)], AMBER, True)
    d.route([(378, 602), (378, 623)], AMBER, True)
    d.route([(378, 602), (378, 612), (617, 612), (617, 623)], AMBER, True)
    for x, tier, title, dates in (
        (32, "hot", "热层 HOT", "10/14 发布"),
        (271, "warm", "温层 WARM", "10/11 至 10/13 发布"),
        (510, "cold", "冷层 COLD", "10/07 至 10/10 发布"),
    ):
        d.node(x, 623, 214, 83, title, [
            (f"{ac[tier]:,} 篇 / {cc[tier]:,} 片", 16, True),
            (dates, 10.5, False)], AMBER, AMBER_BG, True)
    d.box(32, 719, 692, 39, GRAY_BG, RULE)
    d.text(f"日期未知：{ac['unknown']:,} 篇 / {cc['unknown']:,} 片", 46, 726, 12, INK, True)
    d.text("暂不参与年龄分层，不从正文推测结构化时间", 46, 745, 10.3, MUTED)
    d.text("若采用 30 天 / 365 天规则，这 8,000 篇在本次观察日全部为热层。", 32, 767, 10.5, AMBER)

    # D. The independently implemented write lifecycle is a separate backend.
    d.box(792, 512, 365, 268, GRAY_BG, RULE)
    d.text("独立实现：在线生命周期", 809, 524, 15, BLUE, True)
    d.text("显式启用配置；BGE 只读快照不能作为写目标", 809, 549, 10.4, MUTED)
    row_titles = [
        "可信入口：新增 / 更正 / 撤稿 / 显式回热",
        "PG 台账：正文、修订号、状态、索引任务",
        "Temporal Worker：切片 → Embedding",
        "Milvus 热 / 温 / 冷集合：写入并完整验证",
        "PG 条件激活 → 清理旧投影 → 查询状态门禁",
    ]
    for i, title in enumerate(row_titles):
        y = 573 + i*37
        d.box(809, y, 331, 28, white, RULE, radius=6)
        d.text(title, 822, y+7, 10.7, INK, maxw=305)
        if i < len(row_titles)-1:
            d.route([(974, y+28), (974, y+37)], BLUE, width=1.1)
    d.text("年龄降层 + 回热保持期；层间量化尚未接入。", 809, 763, 10.3, MUTED)

    # Human-readable provenance, not an assertion of a new benchmark run.
    d.c.setStrokeColor(RULE)
    d.c.line(32, H-791, 1157, H-791)
    d.text("依据：v2 build_report.json + chunks.jsonl + 检索 YAML；BgeSnapshotVectorIndex / TieredVectorKnowledgeSearchClient / 生命周期代码。", 32, 800, 9.6, MUTED)
    d.text("生成：事实核验 → render_architecture / ReportLab → PDF；校验：单页 + 中文 + 渲染。待做：真实三层索引、迁移与查询实测。", 32, 816, 9.6, MUTED)
    d.text("1 / 1", 1143, 816, 9.6, MUTED, center=True)
    d.c.showPage()
    d.c.save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output/pdf/newsagent-vector-recall-architecture-2026-10-20.pdf")
    args = parser.parse_args()
    facts = collect_facts()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    render_architecture(facts, args.output)
    from pypdf import PdfReader
    reader = PdfReader(args.output)
    assert len(reader.pages) == 1, "the deliverable must be a single page"
    extracted = reader.pages[0].extract_text()
    for required in ("20,187", "50,252", "1,598", "5,268", "7,007", "三层索引尚未构建", "独立实现"):
        assert required in extracted, f"missing expected PDF text: {required}"
    print(json.dumps({"output": str(args.output.resolve()), "pages": len(reader.pages),
                      "articles": dict(facts["articles"]), "chunks": dict(facts["chunks"]),
                      "next_check": "Poppler rendering and visual inspection"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
