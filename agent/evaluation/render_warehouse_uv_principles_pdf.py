"""Create an offline Chinese visual guide to realtime warehouse hot-news principles.

Inputs: the teaching session on UV / time windows / storage, anchored against real
code paths in this repository. No HTTP, database, model calls or .env loading.
Run from repo root with ReportLab installed:
    python agent/evaluation/render_warehouse_uv_principles_pdf.py
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from html import escape
from pathlib import Path

from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "output/pdf/NewsAgent_实时数仓热点UV与时间窗口原理图解.pdf"
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


def verify_sources() -> dict[str, Path]:
    anchors = {
        "warehouse": (ROOT / "agent/app/sql_assistant/warehouse.py",
                      ("unique_users", "news_behavior_aggregate",
                       "news_metric_hourly_tenant_hour_news_idx")),
        "sql_support": (ROOT / "agent/examples/native_hot_news_sql_support.py",
                        ("timedelta(hours=1)", "align to full hours")),
        "understanding": (ROOT / "agent/app/conversation/query_understanding.py",
                          ("window_not_approved",)),
    }
    for name, (path, needles) in anchors.items():
        if not path.is_file():
            raise SystemExit(f"missing anchor file: {path}")
        body = path.read_text(encoding="utf-8")
        missing = [needle for needle in needles if needle not in body]
        if missing:
            raise SystemExit(f"{name} anchor drift: {missing}")
    return {name: path for name, (path, _) in anchors.items()}


class WarehouseUvPrinciplesGuide:
    def __init__(self, output, font, bold_font):
        pdfmetrics.registerFont(TTFont("CN", str(font), subfontIndex=0))
        pdfmetrics.registerFont(TTFont("CN-Bold", str(bold_font), subfontIndex=0))
        self.c = canvas.Canvas(str(output), pagesize=A4, pageCompression=1)
        self.c.setTitle("NewsAgent 实时数仓热点UV与时间窗口原理图解")
        self.c.setAuthor("NewsAgent")
        self.c.setSubject("事件流、时间窗口、UV去重、HLL与Bitmap底层、列存与窗口检索、本地PG对照")
        self.page = 0

    def rect(self, x, y, w, h, color=PALE, radius=9, stroke=None):
        self.c.setFillColor(color)
        self.c.setStrokeColor(stroke or color)
        self.c.roundRect(x, H - y - h, w, h, radius, fill=1, stroke=bool(stroke))

    def text(self, value, x, y, size=11, color=INK, bold=False):
        font = "CN-Bold" if bold else "CN"
        if x + pdfmetrics.stringWidth(str(value), font, size) > W - M + 1:
            raise ValueError(f"page {self.page} text too wide: {value}")
        self.c.setFont(font, size)
        self.c.setFillColor(color)
        self.c.drawString(x, H - y - size * .85, str(value))

    def para(self, value, x, y, w, size=10.5, color=INK, bold=False, maxh=80, leading=None):
        style = ParagraphStyle("cn", fontName="CN-Bold" if bold else "CN", fontSize=size,
                               leading=leading or size * 1.5, textColor=color, wordWrap="CJK")
        p = Paragraph(escape(str(value)).replace("\n", "<br/>"), style)
        _, h = p.wrap(w, H)
        if h > maxh + .5 or y + h > H - 48:
            raise ValueError(f"page {self.page}: paragraph overflow {h}/{maxh}: {value[:70]}")
        p.drawOn(self.c, x, H - y - h)
        return h

    def line(self, x1, y1, x2, y2, color=LINE, width=1):
        self.c.setStrokeColor(color)
        self.c.setLineWidth(width)
        self.c.line(x1, H - y1, x2, H - y2)

    def arrow(self, x1, y1, x2, y2, color=TEAL):
        self.line(x1, y1, x2, y2, color, 1.4)
        p = self.c.beginPath()
        p.moveTo(x2, H - y2)
        if abs(x2 - x1) >= abs(y2 - y1):
            d = 1 if x2 >= x1 else -1
            p.lineTo(x2 - 5 * d, H - y2 + 3)
            p.lineTo(x2 - 5 * d, H - y2 - 3)
        else:
            d = 1 if y2 >= y1 else -1
            p.lineTo(x2 - 3, H - y2 + 5 * d)
            p.lineTo(x2 + 3, H - y2 + 5 * d)
        p.close()
        self.c.setFillColor(color)
        self.c.drawPath(p, fill=1, stroke=0)

    def badge(self, value, x, y, color=TEAL):
        width = pdfmetrics.stringWidth(value, "CN", 9) + 16
        self.rect(x, y, width, 22, Color(color.red, color.green, color.blue, alpha=.10), 5)
        self.text(value, x + 8, y + 6, 9, color)
        return width

    def start(self, section, title, sub):
        self.page += 1
        self.c.bookmarkPage(f"p{self.page}")
        self.c.addOutlineEntry(title, f"p{self.page}", level=0)
        self.text(f"NEWSAGENT / {section}", M, 33, 9, TEAL, True)
        self.text(title, M, 65, 23, NAVY, True)
        self.para(sub, M, 106, CW, 10.5, GRAY, maxh=34)

    def end(self):
        self.line(M, H - 39, W - M, H - 39, LINE, .7)
        self.text("NewsAgent · 实时数仓原理图解 · 2026-10-12", M, H - 28, 8, GRAY)
        self.text(f"{self.page:02d} / {TOTAL:02d}", W - M - 39, H - 28, 8, GRAY)
        self.c.showPage()

    def note(self, title, body, y, h=80, color=TEAL):
        self.rect(M, y, CW, h)
        self.rect(M, y, 4, h, color, 1)
        self.text(title, M + 16, y + 14, 12, color, True)
        self.para(body, M + 16, y + 36, CW - 32, 10.3, maxh=h - 40)

    def box(self, x, y, w, h, title, body, color=TEAL):
        self.rect(x, y, w, h)
        self.text(title, x + 12, y + 12, 12, color, True)
        self.para(body, x + 12, y + 34, w - 24, 10, maxh=h - 39)

    def table(self, y, headers, widths, rows):
        self.rect(M, y, CW, 24, NAVY, 4)
        cx = M
        for header, wd in zip(headers, widths, strict=True):
            self.text(header, cx + 8, y + 7, 9.5, white, True)
            cx += wd
        yy = y + 24
        for i, row in enumerate(rows):
            if i % 2 == 0:
                self.rect(M, yy, CW, 30, PALE, 0)
            cx = M
            for cell, wd in zip(row, widths, strict=True):
                self.text(cell, cx + 8, yy + 9, 9, INK)
                cx += wd
            yy += 30
        return yy

    def cover(self):
        self.start("VISUAL GUIDE", "实时数仓：热点、窗口与UV",
                   "从一次新闻点击，到任意时间窗口的去重人数；拆解企业实时数仓的存储、检索与指标聚合三层原理。")
        x = M
        for label, color in (("通用数仓原理", TEAL), ("本地PG链路对照", BLUE), ("代码锚点已核对", GREEN)):
            x += self.badge(label, x, 151, color) + 9
        bw = (CW - 20) / 3
        stats = (("12KB", "HLL固定空间", "1万到10亿UV，状态大小不变"),
                 ("0.8%", "HLL标准误差", "16384桶；1.04/√m，与基数无关"),
                 ("[s,e)", "半开时间区间", "相邻窗口不重不漏的统一口径"))
        for i, (n, label, body) in enumerate(stats):
            xx = M + i * (bw + 10)
            self.rect(xx, 199, bw, 118)
            self.text(n, xx + 13, 213, 26, TEAL if i != 2 else BLUE, True)
            self.text(label, xx + 13, 252, 11, NAVY, True)
            self.para(body, xx + 13, 275, bw - 26, 9.3, GRAY, maxh=32)
        self.text("三层技术栈，本册聚焦第三层", M, 348, 14, NAVY, True)
        layers = (("存储层 · 数据怎么存", "列存Parquet/ORC、分区、编码压缩、排序键、LSM/MergeTree后台合并。", INK, PALE),
                  ("检索层 · 查询怎么快", "MPP并行执行、向量化计算、谓词下推、分区裁剪、物化视图自动路由。", INK, PALE),
                  ("指标聚合层 · 指标怎么算", "预聚合、固定/滑动窗口、HLL与Bitmap可合并状态、去重语义——本册主线。", TEAL,
                   Color(TEAL.red, TEAL.green, TEAL.blue, alpha=.08)))
        for i, (title, body, color, bg) in enumerate(layers):
            yy = 378 + i * 66
            self.rect(M, yy, CW, 56, bg)
            self.text(title, M + 16, yy + 12, 11.5, color, True)
            self.para(body, M + 16, yy + 33, CW - 32, 9.6, maxh=18)
        self.note("这本图解的阅读顺序",
                  "第2-3页：事件流与时间窗口；第4-6页：UV三方案、HLL与Bitmap底层；第7页：列存格式与窗口检索；第8页：一次点击的字段演化与本地PG对照。",
                  600, 84)
        self.para("本册讲解通用企业数仓原理（Doris/ClickHouse/Redis一类），第8页才对照本项目本地合成链路；不构成企业接口或数仓的验收结论。",
                  M, 706, CW, 9.3, AMBER, maxh=30)
        self.end()

    def events(self):
        self.start("01 / EVENT STREAM", "一次点击，先变成事件流",
                   "不存在“news_id的访问量字段”这种底层存储；每次点击先落成一条不可变事件，计数只是它的衍生视图。")
        self.rect(M, 148, CW, 196, PALE)
        self.text("埋点事件 JSON（最原始的事实）", M + 14, 160, 11, NAVY, True)
        code = ('{',
                '  "event_id":   "e9f3a1c2-7d20-...",  // 幂等：重试不重复计数',
                '  "event_type": "click",',
                '  "news_id":    "N123",',
                '  "tenant_id":  10001,',
                '  "user_id":    "u10086",',
                '  "session_id": "s7b2e90...",',
                '  "event_time": 1770000123456,  // 客户端毫秒时间戳',
                '  "device":     {"os":"iOS","model":"iPhone15"}',
                '}')
        for i, ln in enumerate(code):
            self.text(ln, M + 14, 180 + i * 15.5, 8.8, INK)
        for i, lb in enumerate(("客户端埋点", "网关", "Kafka", "Flink")):
            xx = M + i * 132
            self.rect(xx, 366, 118, 44, PALE, 9, LINE)
            self.text(lb, xx + 59 - pdfmetrics.stringWidth(lb, "CN", 10.5) / 2, 381, 10.5, NAVY, True)
            if i < 3:
                self.arrow(xx + 118, 388, xx + 132, 388)
        self.text("Kafka分区键 = user_id哈希；事件可重放，是真正的事实源", M, 426, 9, GRAY)
        self.box(M, 458, 250, 112, "A · Redis 在线层",
                 '每次点击：ZINCRBY hot:rank:2026101210 1 "N123"；PFADD hot:uv:2026101210 "u10086"。窗口 = key命名，生命周期 = TTL。', TEAL)
        self.box(M + 265, 458, 250, 112, "B · OLAP 分析层",
                 "Flink按(租户,新闻,小时桶)聚合成增量行写入聚合表；存储引擎后台merge按声明聚合，写入方不关心重复。", BLUE)
        self.note("核心原则：明细日志是 source of truth",
                  "计数器是可丢弃的缓存。只存计数，改窗口口径、补UV、按品类重切分都无从重算；存了明细，一切口径都能回放重建。",
                  592, 88)
        self.para("本地项目没有这条事件流：合成数据直接生成小时桶指标，详见第8页。", M, 702, CW, 9.3, AMBER, maxh=26)
        self.end()

    def windows(self):
        self.start("02 / TIME WINDOW", "窗口档位与两种维护方式",
                   "窗口大小没有标准值，档位由指标类型决定；窗口在Redis和数仓里的“本体”完全不同。")
        bottom = self.table(150, ("场景", "典型窗口", "说明"), (78, 158, 279), (
            ("监控告警", "10s / 1min", "计数类速率指标，不做UV去重"),
            ("实时热榜", "滚动5/10min，或近1h滑动", "滑动窗口是主流：粒度保鲜度，跨度保样本量"),
            ("小时桶", "滚动1h", "精确UV的最小实用粒度；与同小时基线对齐"),
            ("天级榜单", "自然日 + T+1回刷", "实时天窗先行，离线修正迟到数据")))
        self.text("窗口的两种本体", M, bottom + 24, 14, NAVY, True)
        self.box(M, bottom + 52, 250, 112, "Redis：窗口 = key命名",
                 "10点的点击只写 ...2026101210 这个key。推进 = TTL到期整key删除；固定窗口直读一个key；任意窗口只能合并预定义桶。", TEAL)
        self.box(M + 265, bottom + 52, 250, 112, "数仓：窗口 = event_time列",
                 "窗口根本不需要维护。查询WHERE event_time过滤，天然任意窗口；时间推进由分区滚动归档管理。", BLUE)
        self.text("选型逻辑", M, bottom + 196, 14, NAVY, True)
        choices = ("窗口越小UV越不可信：分钟级只做计数或HLL近似，精确UV从1h起步。",
                   "滑动窗口避免突变：“近1h每1min滑”防止整点切换时榜单跳变。",
                   "窗口≠数据就绪：水位watermark加迟到容忍5-15min，完整窗口还要再等几分钟。")
        for i, body in enumerate(choices):
            yy = bottom + 224 + i * 44
            self.badge(f"{i + 1:02d}", M, yy, TEAL)
            self.para(body, M + 43, yy + 2, CW - 43, 10, maxh=30)
        self.para("本地链路固定单小时整点桶；三重校验见第8页。", M, bottom + 372, CW, 9.3, AMBER, maxh=26)
        self.end()

    def dedup(self):
        self.start("03 / DEDUP", "UV = 集合基数，三种实现",
                   "去重是集合语义不是数值语义：9-10点UV=100，10-11点UV=100，两桶可能含同一批用户，相加必重复计数。")
        bw = (CW - 20) / 3
        plans = (("HashSet", "精确。存全量ID，O(n)空间；合并要求交换全部明细。适用调试与小数据。", GRAY),
                 ("Bitmap", "精确。一位一用户，popcount即UV；支持交并差。需全局编号，稀疏必须压缩。", BLUE),
                 ("HLL", "近似约0.8%。状态固定12KB；只对并集封闭；合并=逐桶max，代价极小。实时链路主力。", TEAL))
        for i, (title, body, color) in enumerate(plans):
            xx = M + i * (bw + 10)
            self.rect(xx, 150, bw, 150)
            self.text(title, xx + 13, 163, 13, color, True)
            self.para(body, xx + 13, 186, bw - 26, 9.5, maxh=100)
        self.text("跨窗口合并：求并集，不是相加", M, 322, 14, NAVY, True)
        self.table(350, ("方案", "合并方式", "结果"), (120, 160, 235), (
            ("HashSet", "两集合求并集", "精确，但要交换全量明细"),
            ("Bitmap", "逐位 OR", "精确，代价小"),
            ("HLL", "逐桶取 max", "近似（误差1%量级），代价极小")))
        self.note("任意窗口UV的根基：可合并性",
                  "小时桶各存一份可合并状态（Doris HLL列、ClickHouse uniqCombinedState），查询时窗口内的桶现场merge，得到该窗口去重人数。",
                  500, 88)
        self.note("HLL只对并集封闭",
                  "交集没有直接算法，只能容斥：E(A∩B)=E(A)+E(B)-E(A∪B)，误差被放大；需要交集/差集分析时改用Bitmap。",
                  608, 84, AMBER)
        self.end()

    def hll(self):
        self.start("04 / HLL INSIDE", "HLL底层：从抛硬币到12KB",
                   "hash把任意ID变成均匀随机比特串，去重问题就变成抛硬币问题；剩下的工作都是压方差。")
        steps = (
            ("01", "hash均匀化",
             "同一ID永远同一hash（所以能去重）；不同窗口对同一桶取max可合并（所以能并集）。hash质量差直接带来系统偏差。"),
            ("02", "前导零 = 抛硬币",
             "连续k个正面平均需2^(k+1)次抛掷；观察到最大前导零K，则UV约等于2^K。单估计器方差O(n²)，一次好运的hash就能让估计翻倍。"),
            ("03", "分桶随机平均",
             "hash高b位选桶（b=14即m=16384），每个桶独立估计再平均；标准误差降为1.04/√m，且与基数n无关。"),
            ("04", "调和平均 + 两端修正",
             "E = α_m·m² / Σ2^(-M[j])。小基数切LinearCounting（空桶数Z：m·ln(m/Z)）；64bit hash空间大，实际永不饱和。"))
        for i, (num, title, body) in enumerate(steps):
            yy = 150 + i * 94
            self.rect(M, yy, CW, 84)
            self.badge(num, M + 12, yy + 12, TEAL)
            self.text(title, M + 62, yy + 15, 11.5, NAVY, True)
            self.para(body, M + 62, yy + 38, CW - 76, 9.5, maxh=42)
        facts = (("16384桶 × 6bit", "= 12KB固定状态"),
                 ("误差 ≈ 0.81%", "1.04/√16384，与UV规模无关"),
                 ("merge = 逐桶max", "满足交换律与结合律"))
        bw = (CW - 20) / 3
        for i, (n, body) in enumerate(facts):
            xx = M + i * (bw + 10)
            self.rect(xx, 540, bw, 74)
            self.text(n, xx + 13, 553, 11, TEAL, True)
            self.para(body, xx + 13, 576, bw - 26, 9.3, GRAY, maxh=28)
        self.note("合并的正确性",
                  "并集流的每桶最大前导零 = max(两份对应桶)。满足交换与结合律：任意小时桶拼成的大窗口，与在整窗口上直算一遍，分布完全相同。",
                  636, 88)
        self.end()

    def bitmap(self):
        self.start("05 / EXACT BITMAP", "精确一侧：Roaring Bitmap",
                   "32bit空间切成2^16个高16位块；每块按密度三选一容器，空间与速度兼得。")
        bottom = self.table(150, ("容器", "结构", "使用条件"), (150, 185, 180), (
            ("ArrayContainer", "有序uint16数组", "元素 < 4096（稀疏）"),
            ("BitmapContainer", "2^16bit = 8KB定长位图", "≥ 4096（稠密）"),
            ("RunContainer", "游程 (start,length) 对", "连续区间，如1~9999")))
        self.box(M, bottom + 26, CW, 88, "4096这个阈值不是拍脑袋",
                 "数组每个元素2字节，k个元素占2k字节；位图恒定8KB。2k = 8192B 即 k = 4096 时两边打平——按空间自动选容器。", BLUE)
        self.box(M, bottom + 140, CW, 96, "集合操作",
                 "UV = 各容器popcount求和（CPU的POPCNT硬件指令，一周期处理64bit）。并集 = 逐容器OR，只增稠可升级容器；交集 = AND，变稀可降级。",
                 TEAL)
        self.text("精确 vs 近似", M, bottom + 262, 14, NAVY, True)
        self.table(bottom + 290, ("方案", "状态", "误差", "适用"), (120, 150, 80, 165), (
            ("HashSet", "O(n)全量ID", "精确", "调试与小数据"),
            ("Roaring Bitmap", "压缩位图", "精确", "对账、画像圈选、交并差"),
            ("HLL", "12KB固定", "约0.8%", "分钟级实时与任意窗口合并")))
        self.end()

    def storage(self):
        self.start("06 / STORAGE & SEARCH", "列存格式与窗口检索",
                   "数据按列重组、按时间有序；一条时间范围查询经过四层过滤，只付窗口内数据的成本。")
        self.rect(M, 148, CW, 104, PALE)
        self.text("物理布局（按列重组，同列聚集）", M + 14, 160, 11, NAVY, True)
        code = ("news_id    [N123][N124][N123][N125]  → 字典编码成 [1][2][1][3]",
                "event_time [10:00][10:00][10:01][10:02] → 有序；delta+RLE压缩比极高",
                "clicks     [1][1][1][1][1]  → 大段重复；RLE一行搞定")
        for i, ln in enumerate(code):
            self.text(ln, M + 14, 184 + i * 17, 8.8, INK)
        designs = (("排序键", "(event_time, news_id)物理有序；时间有序是窗口检索快的第一功臣。"),
                   ("分区", "按天/月目录级切分；旧分区整目录落冷盘或删除。"),
                   ("分桶", "hash(news_id)水平拆到多机；支撑MPP并行扫描。"))
        bw = (CW - 20) / 3
        for i, (title, body) in enumerate(designs):
            xx = M + i * (bw + 10)
            self.box(xx, 272, bw, 100, title, body, BLUE)
        self.rect(M, 392, CW, 118, PALE)
        self.text("聚合模型：声明合并语义，不更新只追加", M + 14, 404, 11, NAVY, True)
        sql = ("CREATE TABLE hot_hourly (",
               "  tenant_id BIGINT, news_id VARCHAR, event_time DATETIME,",
               "  clicks BIGINT SUM AGGREGATE,    -- 同key重复导入自动SUM",
               "  uv HLL HLL_UNION AGGREGATE      -- 同key自动合并HLL状态",
               ") AGGREGATE KEY(tenant_id, news_id, event_time);")
        for i, ln in enumerate(sql):
            self.text(ln, M + 14, 426 + i * 15.5, 8.8, INK)
        self.para("ClickHouse对应AggregatingMergeTree与uniqMerge；本地PG没有这一层，合成数据一次生成。", M + 14, 490, CW - 28, 8.8, GRAY, maxh=16)
        self.text("窗口检索的四层过滤", M, 528, 14, NAVY, True)
        filters = (("01", "分区裁剪", "时间条件只打开相关分区目录，其余全部跳过。"),
                   ("02", "ZoneMap / 稀疏索引", "每个数据块记录event_time的min/max，几十字节跳过一个块。"),
                   ("03", "排序键定位", "news_id前缀有序，二分定位到连续块。"),
                   ("04", "向量化扫描", "剩余块按列批量加载，SIMD聚合。"))
        for i, (num, title, body) in enumerate(filters):
            yy = 556 + i * 36
            self.badge(num, M, yy, TEAL)
            self.text(title, M + 43, yy + 4, 10.5, NAVY, True)
            self.para(body, M + 190, yy + 4, CW - 190, 9.5, maxh=22)
        self.para("写入走LSM：memtable+WAL → 刷小文件 → 后台merge按声明聚合；“水位”即压实进度——窗口完整性的来源。",
                  M, 716, CW, 9.3, GRAY, maxh=26)
        self.end()

    def local(self):
        self.start("07 / LOCAL CHAIN", "字段演化与本地PG对照",
                   "同一次点击在企业链路与本地链路的落点；本地是“schema语义相同、省掉引擎”的教学实现。")
        self.rect(M, 148, CW, 186, PALE)
        self.text("u10086 点击 N123（10:23:05）之后的字段演化", M + 14, 160, 11, NAVY, True)
        chain = (" ↓ 埋点JSON：10+字段，user_id + 毫秒event_time",
                 " ↓ Kafka：事件原样入分区，key = user_id哈希",
                 " ↓ Flink消费，同一份流分两路",
                 "   A → Redis：ZINCRBY +1 / PFADD 入桶HLL（明细身份仍在）",
                 "   B → 小时桶增量行 → OLAP聚合引擎merge成一行",
                 "        计数列SUM；UV列 = 可合并HLL状态（约12KB）",
                 " ↓ 本地PG：unique_users = 精确bigint（8字节，不可合并）")
        for i, ln in enumerate(chain):
            self.text(ln, M + 14, 184 + i * 19, 8.8, INK)
        self.text("本地链路：dw schema 三表一视图", M, 352, 14, NAVY, True)
        self.table(380, ("对象", "内容", "位置"), (185, 235, 95), (
            ("dw.dim_news", "新闻维表：title/category/publish_time", "warehouse.py"),
            ("dw.news_metric_hourly", "小时桶指标：clicks/unique_users/时长/互动", "warehouse.py"),
            ("dw.news_metric_baseline_hourly", "同小时基线参考均值", "warehouse.py"),
            ("dw.news_behavior_aggregate", "18列冻结视图；契约SHA256校验", "warehouse.py:106")))
        self.para("索引 (tenant_id, event_time, news_id) 见 warehouse.py:447；查询统一半开区间 event_time >= :start AND < :end。",
                  M, 534, CW, 9.3, GRAY, maxh=26)
        self.note("窗口三重校验（native_hot_news_sql_support.py:113）",
                  "恰好1小时 + 整点对齐 + 落在冻结数据集覆盖内；跨小时请求在对话层直接拒绝（query_understanding.py:430，window_not_approved），因为bigint UV无可合并状态。",
                  574, 92)
        self.para("生成链：代码锚点核对 verify_sources() → WarehouseUvPrinciplesGuide / ReportLab → 嵌入中文字体的8页PDF。参数：--output / --font / --bold-font。",
                  M, 688, CW, 9, maxh=32, leading=13)
        self.para("TODO：企业数仓Adapter、真实跨小时去重与调度语义未验收；本册数字为原理示例，非运行数据。",
                  M, 736, CW, 8.2, GRAY, maxh=26)
        self.end()

    def render(self):
        for method in (self.cover, self.events, self.windows, self.dedup,
                       self.hll, self.bitmap, self.storage, self.local):
            method()
        assert self.page == TOTAL
        self.c.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--font", type=Path, default=Path("/System/Library/Fonts/STHeiti Light.ttc"))
    parser.add_argument("--bold-font", type=Path, default=Path("/System/Library/Fonts/STHeiti Medium.ttc"))
    args = parser.parse_args()
    verify_sources()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    WarehouseUvPrinciplesGuide(args.output, args.font, args.bold_font).render()
    print({"output": str(args.output.resolve()), "pages": TOTAL,
           "sha256": sha256(args.output.read_bytes()).hexdigest(), "offline": True})


if __name__ == "__main__":
    main()
