"""Render the verified synthetic operations dataset as a Chinese visual PDF.

Run from repository root with a Python that includes reportlab:
    python agent/evaluation/render_operations_pdf.py
The input manifest is verified before rendering. No database/model/network access.
"""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
from html import escape
import json
from pathlib import Path

from reportlab.lib.colors import HexColor, Color, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "agent/evaluation/datasets/hot_news/operations_synthetic_v1"
DEFAULT_OUTPUT = ROOT / "output/pdf/DataLoop_运营反馈与数据集扩充_可视化指南.pdf"
WIDTH, HEIGHT = A4
MARGIN = 42
CONTENT = WIDTH - MARGIN * 2
TOTAL_PAGES = 13
NAVY = HexColor("#16324A")
INK = HexColor("#263F52")
MUTED = HexColor("#667D8D")
TEAL = HexColor("#087F8C")
BLUE = HexColor("#407BB9")
RED = HexColor("#C9554C")
AMBER = HexColor("#A86B14")
GREEN = HexColor("#2B856B")
PALE = HexColor("#F2F6F8")
LINE = HexColor("#DCE5EA")
ACT = {"accepted": ("采纳", GREEN), "rejected": ("驳回", RED), "deferred": ("延后", AMBER), "corrected": ("修正", BLUE)}
CAT = {"analysis_error": "分析错误", "editorial_judgment": "编辑判断", "strategy_judgment": "策略判断", "positive_example": "正向案例"}
LAYER = {"golden": ("Golden", TEAL), "fresh_bad_case": ("Fresh", RED), "high_risk": ("High-risk", AMBER)}
STRATEGIES = [
    ("新闻价值 / 证据", "是否有新增公共价值，来源能否核验？", "旧照片高热，不等于值得通知。"),
    ("突发通报", "范围与时段明确，数字和归因可核验。", "人数未公布，不能写成确认人数。"),
    ("地域人群", "地方信息匹配受影响区域与获准分群。", "一条航线变化，不应扩大到全站。"),
    ("专题订阅", "类别、授权和最新撤销状态均匹配。", "历史兴趣不能覆盖当前退订。"),
    ("事件增量", "区分重要新增事实与文章改写。", "换稿件ID或标题，不等于新事件。"),
    ("时效 / 时区", "当前仍有价值，发送时间换算正确。", "昨日公告、错时区、静默期须重审。"),
    ("频控 / 摘要", "可信余量允许触达，形式符合偏好。", "额度用尽可拒绝；多条可合摘要。"),
    ("文案 / 断言", "保留条件，计划和事实保持区别。", "拟建设不能表达为已投产。"),
    ("渠道 / 隐私", "尊重频道选择，保护锁屏个人事项。", "换频道不能绕过用户关闭。"),
    ("摘要增量 / 窗口", "整理有价值，保留各项条件和时效。", "重复标题或删除条件仍须修正。"),
    ("更正 / 平台回执", "更正透明，内容状态和通知状态分开。", "正文删除不证明锁屏已撤回。"),
    ("分母 / 因果边界", "统计口径与观察窗完整，解释有边界。", "CTR变化不能证明真实策略收益。"),
]


def load_input(directory: Path) -> tuple[dict, dict]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, spec in manifest["files"].items():
        if Path(name).name != name:
            raise ValueError("manifest must only reference sibling files")
        payload = (directory / name).read_bytes()
        if sha256(payload).hexdigest() != spec["sha256"] or len(payload) != spec["bytes"]:
            raise ValueError(f"source artifact differs: {name}")
    data = json.loads((directory / "operations_cases.json").read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) == manifest["case_count"] == 48
    assert data["synthetic"] is True and data["annotation_status"] == "generated_pending_review"
    assert Counter(c["proposed_layer"] for c in cases) == Counter(manifest["layers"])
    assert Counter(c["scenario"]["expected"]["decision_type"] for c in cases) == Counter(manifest["decisions"])
    assert len({c["pair_id"] for c in cases}) == 24
    assert all(c["delivery_status"] == "not_executed" for c in cases)
    return data, manifest


class Guide:
    def __init__(self, path: Path, data: dict, manifest: dict, font: Path, bold_font: Path):
        pdfmetrics.registerFont(TTFont("CN", str(font), subfontIndex=0))
        pdfmetrics.registerFont(TTFont("CN-Bold", str(bold_font), subfontIndex=0))
        self.c = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
        self.c.setTitle("DataLoop 运营反馈与数据集扩充 - 可视化指南")
        self.c.setAuthor("NewsAgent / 合成运营数据集")
        self.c.setSubject("48个合成运营案例、12类策略、候选评测分层与正式链路")
        self.data = data
        self.manifest = manifest
        self.cases = data["cases"]
        self.page = 0

    def rect(self, x, y, w, h, fill=PALE, radius=10, stroke=None):
        self.c.setFillColor(fill)
        self.c.setStrokeColor(stroke or fill)
        self.c.roundRect(x, HEIGHT - y - h, w, h, radius, fill=1, stroke=bool(stroke))

    def text(self, value, x, y, size=11, color=INK, bold=False):
        self.c.setFont("CN-Bold" if bold else "CN", size)
        self.c.setFillColor(color)
        self.c.drawString(x, HEIGHT - y - size * .85, str(value))

    def para(self, value, x, y, w, size=11, color=INK, bold=False, maxh=None, leading=None):
        style = ParagraphStyle(
            "body", fontName="CN-Bold" if bold else "CN", fontSize=size,
            leading=leading or size * 1.5, textColor=color, wordWrap="CJK", spaceAfter=0,
        )
        paragraph = Paragraph(escape(str(value)).replace("\n", "<br/>"), style)
        _, h = paragraph.wrap(w, HEIGHT)
        if maxh is not None and h > maxh + .5:
            raise ValueError(f"page {self.page}: text box overflow ({h}>{maxh}): {str(value)[:70]}")
        if y + h > HEIGHT - 43:
            raise ValueError(f"page {self.page}: text extends into footer: {str(value)[:70]}")
        paragraph.drawOn(self.c, x, HEIGHT - y - h)
        return h

    def arrow(self, x1, y1, x2, y2, color=TEAL, dashed=False):
        self.c.setStrokeColor(color)
        self.c.setLineWidth(1.6)
        self.c.setDash(4, 3) if dashed else self.c.setDash()
        self.c.line(x1, HEIGHT-y1, x2, HEIGHT-y2)
        self.c.setDash()
        self.c.setFillColor(color)
        path = self.c.beginPath()
        if abs(x2-x1) >= abs(y2-y1):
            direction = 1 if x2 > x1 else -1
            path.moveTo(x2, HEIGHT-y2)
            path.lineTo(x2-direction*6, HEIGHT-y2+3)
            path.lineTo(x2-direction*6, HEIGHT-y2-3)
        else:
            direction = 1 if y2 > y1 else -1
            path.moveTo(x2, HEIGHT-y2)
            path.lineTo(x2-3, HEIGHT-y2+direction*6)
            path.lineTo(x2+3, HEIGHT-y2+direction*6)
        path.close()
        self.c.drawPath(path, fill=1, stroke=0)

    def badge(self, label, x, y, color=TEAL, width=None):
        w = width or pdfmetrics.stringWidth(label, "CN", 9) + 18
        self.rect(x, y, w, 22, Color(color.red, color.green, color.blue, alpha=.1), radius=5)
        self.text(label, x+9, y+6, 9, color)
        return w

    def start(self, section, title, subtitle=""):
        self.page += 1
        self.c.bookmarkPage(f"page-{self.page}")
        self.c.addOutlineEntry(title, f"page-{self.page}", level=0, closed=False)
        self.text(f"NEWSAGENT / {section}", MARGIN, 39, 9, TEAL, True)
        self.text(title, MARGIN, 69, 24, NAVY, True)
        if subtitle:
            self.para(subtitle, MARGIN, 108, CONTENT, 10.5, MUTED, maxh=38)

    def end(self):
        self.c.setStrokeColor(LINE)
        self.c.setLineWidth(.6)
        self.c.line(MARGIN, 39, WIDTH-MARGIN, 39)
        self.text("NewsAgent · 合成运营反馈 · 待复核 / 未投递", MARGIN, HEIGHT-29, 8, MUTED)
        self.text(f"{self.page:02d} / {TOTAL_PAGES:02d}", WIDTH-MARGIN-39, HEIGHT-29, 8, MUTED)
        self.c.showPage()

    def note(self, title, body, y, height=84, color=TEAL):
        self.rect(MARGIN, y, CONTENT, height, PALE)
        self.rect(MARGIN, y, 4, height, color, radius=1)
        self.text(title, MARGIN+18, y+16, 13, color, True)
        self.para(body, MARGIN+18, y+40, CONTENT-36, 10.5, maxh=height-45)

    def overview(self):
        self.start("VISUAL GUIDE · 2026.10.10", "DataLoop：让运营经验变成数据", "从一次新闻建议，走到可复核、可回放、可持续扩充的案例集。")
        x = MARGIN
        for label in ("合成新闻情景", "模拟编辑意见", "候选评测分层"):
            x += self.badge(label, x, 148) + 9
        stats = [("48", "模拟反馈案例"), ("24", "合成对照组"), ("12", "运营策略类别")]
        w = (CONTENT-24)/3
        for i, (count, label) in enumerate(stats):
            x = MARGIN+i*(w+12)
            self.rect(x, 193, w, 103, PALE)
            self.text(count, x+17, 205, 39, TEAL, True)
            self.text(label, x+18, 262, 11, MUTED)
        self.text("你关注的扩充方向", MARGIN, 330, 15, NAVY, True)
        labels = ["新闻事实", "运营判断", "评测案例", "配置改进"]
        bw = (CONTENT-45)/4
        for i, label in enumerate(labels):
            x = MARGIN+i*(bw+15)
            self.rect(x, 362, bw, 54, TEAL if i == 2 else PALE)
            self.text(label, x+13, 382, 14, white if i == 2 else NAVY, True)
            if i < 3: self.arrow(x+bw+2, 389, x+bw+12, 389)
        self.para("重点保留：当时知道什么、为什么这样决定、哪些条件改变后需要重新判断。", MARGIN, 433, CONTENT, 11, maxh=38)
        self.text("四种运营决定 · 案例数量", MARGIN, 500, 14, NAVY, True)
        maximum = max(self.manifest["decisions"].values())
        for i, decision in enumerate(ACT):
            label, color = ACT[decision]
            y = 536+i*43
            n = self.manifest["decisions"][decision]
            self.text(label, MARGIN, y+4, 12, color, True)
            self.rect(MARGIN+66, y, 350, 20, PALE, radius=4)
            self.rect(MARGIN+66, y, 350*n/maximum, 20, color, radius=4)
            self.text(str(n), MARGIN+434, y+4, 12, color, True)
        self.para("这里统计的是合成素材数量，不是团队采纳率或真实推送效果。", MARGIN, 719, CONTENT, 10, MUTED)
        self.para("阅读路线：1-8页看链路与判断；9-12页查48条案例；13页看来源与复现。", MARGIN, 749, CONTENT, 10, MUTED)
        self.end()

    def chain(self):
        self.start("01 / THE LOOP", "正式链路与本批素材的位置", "运营决定、标签二审、配置晋升分别保留自己的审计与审批边界。")
        self.badge("正式 DataLoop", MARGIN, 153, TEAL)
        rows = [
            [("已完成热点运行", "报告、新闻身份、配置版本"), ("运营决定", "采纳 / 驳回 / 延后 / 修正"), ("FeedbackCase", "同事务回流，待标注")],
            [("提交标注", "分析对错与最小评测预期"), ("独立复核", "approve_label，形成批准标签"), ("冻结三层数据", "版本、快照与内容哈希")],
            [("离线回放", "当前 / 候选 / 上一实验配置"), ("Python Gate", "按门槛及相对退化判断"), ("人工配置审批", "批准后激活，超时不激活")],
        ]
        bw = (CONTENT-30)/3
        for r, row in enumerate(rows):
            y = 194+r*117
            for i, (title, body) in enumerate(row):
                x = MARGIN+i*(bw+15)
                self.rect(x, y, bw, 90, PALE)
                self.text(f"{r*3+i+1:02d}", x+12, y+11, 9, TEAL, True)
                self.text(title, x+12, y+30, 12, NAVY, True)
                self.para(body, x+12, y+52, bw-24, 9.5, MUTED, maxh=33)
                if i < 2: self.arrow(x+bw+1, y+45, x+bw+13, y+45)
            if r < 2:
                # Continue from the rightmost box to the next row's leftmost box.
                self.c.setStrokeColor(TEAL)
                self.c.setLineWidth(1)
                turn = y+102
                self.c.line(WIDTH-MARGIN-12, HEIGHT-y-90, WIDTH-MARGIN-12, HEIGHT-turn)
                self.c.line(WIDTH-MARGIN-12, HEIGHT-turn, MARGIN+12, HEIGHT-turn)
                self.arrow(MARGIN+12, turn, MARGIN+12, y+117)
        self.para("激活后的配置由下一次热点运行读取；需要时可显式回滚。", MARGIN, 530, CONTENT, 10, MUTED)
        self.badge("本批：离线合成分支", MARGIN, 574, AMBER)
        labels = [("版本化情景", "事实 + 候选建议"), ("程序计算与校验", "聚合指标 + 身份 + 分层"), ("离线素材与评测", "案例包 + 盲输入 + 评分")]
        for i, (title, body) in enumerate(labels):
            x = MARGIN+i*(bw+15)
            self.rect(x, 613, bw, 76, HexColor("#FBF4E8"))
            self.text(title, x+12, 629, 11, AMBER, True)
            self.para(body, x+12, 652, bw-24, 9.5, MUTED, maxh=30)
            if i < 2: self.arrow(x+bw+1, 650, x+bw+13, 650, AMBER)
        self.note("接入正式账本的下一步", "先在隔离租户建立合法已完成的run/report，再做导入映射、标签独立二审与冻结。目前48例仍是离线素材。", 718, 78, AMBER)
        self.end()

    def strategies(self):
        self.start("02 / EDITORIAL STRATEGIES", "12类策略：先判断，再决定", "以下为项目场景与公开原则的映射；每类4例，不代表腾讯新闻内部操作规范。")
        cw = (CONTENT-24)/3
        for i, (title, rule, counter) in enumerate(STRATEGIES):
            x = MARGIN+(i%3)*(cw+12)
            y = 158+(i//3)*148
            self.rect(x, y, cw, 136, PALE)
            self.text(f"OPS-{i+1:02d} / 4例", x+12, y+12, 9, TEAL, True)
            self.para(title, x+12, y+35, cw-24, 12, NAVY, True, maxh=39)
            self.para(rule, x+12, y+62, cw-24, 9.8, maxh=34)
            self.para(counter, x+12, y+99, cw-24, 9.1, MUTED, maxh=29)
        self.para("共同前提：可信事实、获准受众、当前许可、可用预算、有效时段与可追溯版本。", MARGIN, 766, CONTENT, 9.5, MUTED)
        self.end()

    def classification(self):
        self.start("03 / TWO DIMENSIONS", "运营动作与问题分类，是两个维度", "同一个“驳回”可能来自事实错误、编辑价值或策略条件；不能直接当成模型错例。")
        self.text("本批交叉分布 · 单位：例", MARGIN, 163, 13, NAVY, True)
        cats = ["analysis_error", "editorial_judgment", "strategy_judgment", "positive_example"]
        left, top, labelw = MARGIN, 202, 77
        cellw = (CONTENT-labelw)/4
        for j, cat in enumerate(cats): self.text(CAT[cat], left+labelw+j*cellw+8, top+11, 11, MUTED, True)
        for i, decision in enumerate(ACT):
            y = top+44+i*53
            label, color = ACT[decision]
            self.text(label, left+9, y+18, 12, color, True)
            for j, cat in enumerate(cats):
                n = sum(c["scenario"]["expected"]["decision_type"] == decision and c["scenario"]["expected"]["operator_feedback"]["category"] == cat for c in self.cases)
                x = left+labelw+j*cellw
                shade = HexColor("#D7ECEE") if n else PALE
                self.rect(x+2, y, cellw-5, 46, shade, radius=5)
                self.text(str(n), x+cellw/2-7, y+12, 21, TEAL if n else MUTED, bool(n))
        self.para("0仅表示这批素材未出现该组合，不代表业务上禁止。", MARGIN, 472, CONTENT, 9.5, MUTED)
        self.note("事实正确，也可能不适合推", "地方轮渡公告可信，但候选推向全站：驳回 / 策略判断。保留分析为正确，调整受众。", 519, 84, TEAL)
        self.note("文案修正，要看错误在哪一层", "补助试点的分析保留申请条件，通知标题却写“人人可领”：修正 / 编辑判断。修改文案，不把分析标成坏例。", 618, 90, BLUE)
        self.para("Fresh候选：分析错误 + incorrect/partially_correct。正式入集仍需独立标签复核。", MARGIN, 740, CONTENT, 10.5, RED, True, maxh=33)
        self.end()

    def budget(self):
        self.start("04 / CONTRAST CASE", "同一条新闻，预算变化会改变决定", "案例组 ops07-budget-state：两例的新闻事实和分析均正确，差别在运营条件。")
        self.note("共同事实", "虚构云杉市艺术馆预约入口与日期已确认，目标是展览订阅者。下列“滚动24小时最多2条”是本地模拟假设。", 157, 86)
        cases = [c for c in self.cases if c["pair_id"] == "ops07-budget-state"]
        cw = (CONTENT-18)/2
        for i, case in enumerate(cases):
            x = MARGIN+i*(cw+18)
            expected = case["scenario"]["expected"]
            action, color = ACT[expected["decision_type"]]
            self.rect(x, 274, cw, 341, PALE)
            self.badge(f"变体 {'A' if i == 0 else 'B'} · {action}", x+15, 291, color)
            self.text(f"已使用 {i+1} / 2 条", x+16, 337, 23, color, True)
            for unit in range(2):
                self.rect(x+17+unit*79, 383, 67, 16, color if unit < i+1 else LINE, radius=4)
            self.text("预算尚有余额" if i == 0 else "额度已满，无获准例外", x+16, 420, 11, NAVY, True)
            self.para(expected["reason"], x+16, 454, cw-32, 11, maxh=54)
            self.para(f"反馈类别：{CAT[expected['operator_feedback']['category']]}\n分析判断：正确\n投递状态：未执行", x+16, 529, cw-32, 10.5, MUTED, maxh=70)
        self.note("为何右侧不是分析新错例？", "新闻事实没有错，运营条件限制了触达。应沉淀频控反例与适用条件，而不是让模型改写事实或伪造预算例外。", 649, 96, AMBER)
        self.para("采纳表示认可模拟建议；频控快照不是实际配额预留，也没有形成发送回执。", MARGIN, 768, CONTENT, 9.2, MUTED, maxh=18)
        self.end()

    def errors(self):
        self.start("05 / WHERE IS THE ERROR", "区分分析越界与文案误导", "记录错误发生的位置，才能把案例送到合适的评测层。以下新闻与机构均为虚构。")
        selections = [
            ("ops02-breaking-analysis-errors", "invented-count", "A / 分析主张超出证据", "应急通报只确认火情，人数正在核实。", "analysis_claim", RED),
            ("ops08-headline-context", "clickbait", "B / 分析正确，通知文案误导", "补助是限定试点，仅两类申请者可申请且需要审核。", "copy", BLUE),
        ]
        for i, (pair, variant, heading, facts, claim_field, color) in enumerate(selections):
            case = next(c for c in self.cases if c["pair_id"] == pair and c["scenario"]["variant_id"] == variant)
            scenario, y = case["scenario"], 155+i*247
            self.rect(MARGIN, y, CONTENT, 232, PALE)
            self.text(heading, MARGIN+16, y+15, 13, color, True)
            self.para("已知事实："+facts, MARGIN+16, y+45, CONTENT-32, 10.5, maxh=35)
            self.para("候选表达："+scenario["candidate"][claim_field], MARGIN+16, y+88, CONTENT-32, 10.5, color, maxh=49)
            expected = scenario["expected"]
            self.para(f"模拟决定：{ACT[expected['decision_type']][0]} / {CAT[expected['operator_feedback']['category']]}\n复核理由：{expected['reason']}", MARGIN+16, y+151, CONTENT-32, 10.5, maxh=66)
        self.note("再看一个数值错例：汇总CTR用总分母", "历史合成观察：甲50/100，乙90/900。汇总应为140/1000 = 14%；简单平均50%与10%得到30%是错误分析。", 674, 97, RED)
        self.end()

    def layers(self):
        self.start("06 / DATASET LAYERS", "三层候选集，各自回答一个问题", "所有层均待复核。Golden含稳定正负例；Fresh聚焦分析错误；High-risk检验风险与边界。")
        x = MARGIN
        for layer in ("golden", "fresh_bad_case", "high_risk"):
            label, color = LAYER[layer]
            n = self.manifest["layers"][layer]
            w = CONTENT*n/48
            self.rect(x, 159, w, 39, color, radius=0)
            self.text(f"{label} {n}", x+10, 171, 11, white, True)
            x += w
        specs = [
            ("golden", "稳定判断能否保持？", "常见场景的采纳、驳回、延后和修正。检验新增配置是否破坏已有合理判断。", "12对照组 / 24例"),
            ("fresh_bad_case", "明确分析错误能否纠正？", "人数与归因、事件混淆、无证据断言、分母与因果等8例。均为分析错误候选。", "4对照组 / 8例"),
            ("high_risk", "边界条件能否守住？", "未知许可、隐私、时区、摘要条件、更正回执等。包含另外2例分析错误。", "8对照组 / 16例"),
        ]
        for i, (layer, title, body, group) in enumerate(specs):
            y = 232+i*136
            label, color = LAYER[layer]
            self.rect(MARGIN, y, CONTENT, 119, PALE)
            self.text(label, MARGIN+16, y+15, 17, color, True)
            self.text(group, MARGIN+322, y+19, 10, MUTED)
            self.text(title, MARGIN+16, y+48, 12, NAVY, True)
            self.para(body, MARGIN+16, y+73, CONTENT-32, 10.5, maxh=35)
        self.note("按组隔离，而不是随机拆行", "同一对照组的两例与证据保持在同一层。后续若用于训练与评测，还要按事件血缘做隔离，避免同事件改稿跨集合。", 669, 96, TEAL)
        self.end()

    def expansion(self):
        self.start("07 / EXPAND THE DATA", "扩充时，先做可解释的成对案例", "用最小条件变化检验决定是否合理；保留模拟来源、版本、证据与未执行状态。")
        steps = [
            ("01", "设计新闻场景", "明确主体、事件、时段、来源和影响范围。虚构情景显式标记。"),
            ("02", "只改一个关键条件", "例如额度1变2、有效变过期、订阅有效变撤销，形成可解释对照。"),
            ("03", "生成模拟运营意见", "保留动作、问题类别、理由码、简评与修订；判断分析是否出错。"),
            ("04", "校验并去掉答案提示", "复算CTR/热度，检查ID与分层；盲输入不能含预期或作者的对错提示。"),
            ("05", "另建版本并复核", "重建文件与哈希清单，做盲预测与人工校准，再决定正式标注用途。"),
        ]
        for i, (number, title, body) in enumerate(steps):
            y = 156+i*91
            self.rect(MARGIN, y, 36, 36, TEAL, radius=8)
            self.text(number, MARGIN+8, y+11, 13, white, True)
            self.text(title, MARGIN+53, y+1, 13, NAVY, True)
            self.para(body, MARGIN+53, y+25, CONTENT-53, 10.5, maxh=35)
            if i < 4: self.arrow(MARGIN+18, y+44, MARGIN+18, y+81, LINE)
        self.note("本批已能做", "阅读48条案例、校验版本与哈希、读取分析评测投影、按完整分母计算运营预测与模拟参考的一致性。", 620, 78, TEAL)
        self.note("正式接入仍待完成", "隔离租户run/report、素材导入映射、标签二审、正式冻结；真实预算/偏好/投递与运营策略模型评测另行接入。", 710, 78, AMBER)
        self.end()

    def index(self, offset):
        first = offset+1
        last = offset+12
        self.start(f"APPENDIX / CASES {first:02d}-{last:02d}", "48条模拟反馈 · 快速索引", "每条显示标题、模拟决定、问题类别与理由；完整事实和修订保存在案例JSON与中文案例册。")
        subset = self.cases[offset:offset+12]
        for i, case in enumerate(subset):
            y = 150+i*52
            scenario = case["scenario"]
            expected = scenario["expected"]
            action, color = ACT[expected["decision_type"]]
            self.rect(MARGIN, y, CONTENT, 47, PALE, radius=5)
            self.text(f"{first+i:02d}", MARGIN+8, y+9, 10, TEAL, True)
            self.text(action, MARGIN+36, y+9, 10, color, True)
            self.para(scenario["title"].replace("（虚构）", ""), MARGIN+72, y+7, CONTENT-84, 9.7, NAVY, True, maxh=15, leading=13)
            self.text(case["strategy_id"]+" · "+CAT[expected["operator_feedback"]["category"]], MARGIN+8, y+29, 8, MUTED)
            self.para(expected["reason"], MARGIN+143, y+25, CONTENT-156, 8.5, MUTED, maxh=21, leading=10.2)
        self.para("全部情景均为虚构；“采纳”没有产生真实审批或发送；索引序号按源目录稳定顺序排列。", MARGIN, 786, CONTENT, 8.5, MUTED, maxh=13)
        self.end()

    def sources(self):
        self.start("REFERENCE / REPRODUCE", "研究依据与生成方式", "公开资料支撑编辑与通知原则；情景规则是工程映射，具体参数仍为本地模拟假设。")
        for i, source in enumerate(self.data["sources"]):
            y = 153+i*41
            self.text(f"{i+1:02d}", MARGIN, y+1, 9.5, TEAL, True)
            h = self.para(source["title"], MARGIN+28, y, CONTENT-28, 9.5, NAVY, maxh=29, leading=13)
            self.c.linkURL(source["url"], (MARGIN+28, HEIGHT-y-h, WIDTH-MARGIN, HEIGHT-y+2), relative=0)
        self.text("点击来源标题可打开原文；查阅日期：2026-10-10。", MARGIN, 528, 9.5, MUTED)
        self.rect(MARGIN, 561, CONTENT, 188, PALE)
        self.text("本PDF的完整生成链", MARGIN+16, 577, 12, NAVY, True)
        self.para("上游：已校验的operations_cases.json + manifest.json\n转换：读取48例、复核文件哈希、统计数量和分类、提取案例对照\n核心：load_input() + Guide；ReportLab绘制中文、图表和案例索引\n输出：可搜索文字、嵌入中文字体、13页PDF与目录书签", MARGIN+16, 602, CONTENT-32, 9.5, maxh=85, leading=15)
        self.para("复现入口：python agent/evaluation/render_operations_pdf.py\n参数：--input-dir / --output / --font / --bold-font\n依赖：Python、ReportLab；布局验证：Poppler渲染 + PDF文字/字体检查。", MARGIN+16, 692, CONTENT-32, 9, MUTED, maxh=49, leading=14)
        self.para("源数据版本："+self.data["dataset_version"]+"。PDF为静态阅读版；正式导入与标签二审仍待完成。", MARGIN, 767, CONTENT, 9, MUTED, maxh=28)
        self.end()

    def render(self):
        self.overview()
        self.chain()
        self.strategies()
        self.classification()
        self.budget()
        self.errors()
        self.layers()
        self.expansion()
        for offset in (0, 12, 24, 36): self.index(offset)
        self.sources()
        assert self.page == TOTAL_PAGES
        self.c.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--font", type=Path, default=Path("/System/Library/Fonts/STHeiti Light.ttc"))
    parser.add_argument("--bold-font", type=Path, default=Path("/System/Library/Fonts/STHeiti Medium.ttc"))
    args = parser.parse_args()
    data, manifest = load_input(args.input_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    Guide(args.output, data, manifest, args.font, args.bold_font).render()
    print(json.dumps({"output": str(args.output), "pages": TOTAL_PAGES, "cases": len(data["cases"]), "source_version": data["dataset_version"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
