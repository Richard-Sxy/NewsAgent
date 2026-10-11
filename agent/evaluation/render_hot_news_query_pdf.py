"""Render the archived hot-news query diagnosis and dialogue evidence offline.

No model, HTTP, database, or dotenv access. ReportLab embeds the supplied Chinese
fonts; summary counts, examples, and links come from the saved evaluation JSON.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
from html import escape
import json
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "output/evaluation/hot-news-dialogue-v1/dialogue-report.json"
OUTPUT = ROOT / "output/pdf/NewsAgent_热点查询修复与对话评测图解.pdf"
W, H = A4
M = 42
CW = W - 2*M
NAVY = HexColor("#153348")
INK = HexColor("#2D485B")
MUTED = HexColor("#62798A")
TEAL = HexColor("#078478")
BLUE = HexColor("#316CCA")
AMBER = HexColor("#A96B12")
PALE = HexColor("#EFF5F8")
LINE = HexColor("#D8E3EA")
TOTAL = 5


def load_evidence(path: Path):
    report = json.loads(path.read_text(encoding="utf-8"))
    cases = report["cases"]
    counts = Counter(item["verdict"] for item in cases)
    summary = report["summary"]
    assert report["scope"] == "isolated_synthetic_local_rule_port"
    assert len(cases) == summary["case_count"] == 30
    assert sum(len(item["turns"]) for item in cases) == summary["turn_count"] == 33
    assert counts["passed"] == summary["passed"] == 29
    assert counts["expected_pending"] == summary["expected_pending"] == 1
    assert summary["failed"] == summary["blocked"] == 0
    query_traces = [trace for case in cases for turn in case["turns"]
                    for trace in (turn.get("response") or {}).get("tools", [])
                    if trace["name"] == "query_hot_news" and trace["status"] == "completed"]
    assert len(query_traces) == summary["completed_query_count"] == 12
    assert len({trace["result"]["run_id"] for trace in query_traces}) == 11
    assert len({trace["result"]["sql_query_id"] for trace in query_traces}) == 11
    manifest_path = ROOT / "agent/evaluation/datasets/hot_news_dialogue_v1/manifest.json"
    cases_path = manifest_path.with_name("cases.json")
    manifest = json.loads(manifest_path.read_text())
    assert sha256(cases_path.read_bytes()).hexdigest() == manifest["sha256"] == report["dataset_sha256"]
    warehouse = json.loads((ROOT / "agent/data/timeline-v4/manifest.json").read_text())
    assert warehouse["synthetic"] and warehouse["dataset_profile"] == "timeline-v4"
    assert warehouse["hours_per_news"] == 720 and warehouse["news_per_tenant"] == 1200
    assert warehouse["total_tenants"] == 2
    assert warehouse["total_metric_row_count"] == warehouse["total_baseline_row_count"] == 1728000
    report["_warehouse_manifest"] = warehouse
    assert (ROOT / "agent/docs/hot-news-query-diagnosis.md").is_file()
    return report, query_traces


class QueryGuide:
    def __init__(self, output, report, queries, font, bold_font):
        pdfmetrics.registerFont(TTFont("CN", str(font), subfontIndex=0))
        pdfmetrics.registerFont(TTFont("CN-Bold", str(bold_font), subfontIndex=0))
        self.c = canvas.Canvas(str(output), pagesize=A4, pageCompression=1)
        self.c.setTitle("NewsAgent 热点查询修复与对话评测图解")
        self.c.setAuthor("NewsAgent")
        self.c.setSubject("调用失败定位、小时窗口、存储迁移、真实对话评测及能力边界")
        self.report, self.queries = report, queries
        self.cases = {case["id"]: case for case in report["cases"]}
        self.page = 0

    def rect(self, x, y, w, h, fill=PALE, radius=8):
        self.c.setFillColor(fill)
        self.c.roundRect(x, H-y-h, w, h, radius, fill=1, stroke=0)

    def text(self, value, x, y, size=11, color=INK, bold=False):
        font = "CN-Bold" if bold else "CN"
        if x + pdfmetrics.stringWidth(str(value), font, size) > W-M+1:
            raise ValueError(f"page {self.page} line overflow: {value}")
        self.c.setFillColor(color)
        self.c.setFont(font, size)
        self.c.drawString(x, H-y-size*.85, str(value))

    def para(self, value, x, y, width, size=10.5, color=INK, maxh=70, bold=False, leading=None):
        style = ParagraphStyle("body", fontName="CN-Bold" if bold else "CN", fontSize=size,
                               leading=leading or size*1.5, textColor=color, wordWrap="CJK")
        p = Paragraph(escape(value).replace("\n", "<br/>"), style)
        _, height = p.wrap(width, H)
        if height > maxh+.5 or y+height > H-47:
            raise ValueError(f"page {self.page} paragraph overflow {height}/{maxh}: {value[:80]}")
        p.drawOn(self.c, x, H-y-height)
        return height

    def line(self, x1, y1, x2, y2, color=LINE, width=1):
        self.c.setStrokeColor(color)
        self.c.setLineWidth(width)
        self.c.line(x1, H-y1, x2, H-y2)

    def arrow(self, x1, y1, x2, y2):
        self.line(x1, y1, x2, y2, TEAL, 1.4)
        p = self.c.beginPath()
        p.moveTo(x2, H-y2)
        if x1 == x2:
            p.lineTo(x2-3, H-y2+5)
            p.lineTo(x2+3, H-y2+5)
        else:
            p.lineTo(x2-5, H-y2+3)
            p.lineTo(x2-5, H-y2-3)
        p.close()
        self.c.setFillColor(TEAL)
        self.c.drawPath(p, fill=1, stroke=0)

    def badge(self, label, x, y, color=TEAL):
        self.rect(x, y, 68, 22, HexColor("#E5F3F0") if color == TEAL else HexColor("#FBF0DC"), 5)
        self.text(label, x+9, y+6, 9, color, True)

    def start(self, label, title, subtitle):
        self.page += 1
        self.c.bookmarkPage(f"p{self.page}")
        self.c.addOutlineEntry(title, f"p{self.page}", level=0)
        self.text("NEWSAGENT / "+label, M, 32, 9, TEAL, True)
        self.text(title, M, 64, 25, NAVY, True)
        self.para(subtitle, M, 106, CW, 10.5, MUTED, maxh=37)

    def end(self):
        self.line(M, H-40, W-M, H-40, LINE, .7)
        self.text("NewsAgent · 热点调用修复 · 2026-10-10", M, H-28, 8, MUTED)
        self.text(f"{self.page:02d} / {TOTAL:02d}", W-M-40, H-28, 8, MUTED)
        self.c.showPage()

    def link(self, label, url, x, y, width=CW, size=9.5):
        h = self.para(label, x, y, width, size, BLUE, maxh=35)
        self.c.linkURL(url, (x, H-y-h, x+width, H-y), relative=0, thickness=0)
        return h

    def cover(self):
        self.start("01 / RESULTS", "这次我完成了什么", "定位调用故障，修复本地链路，构建冻结评测集，并实际与热点 Agent 对话。")
        self.rect(M, 159, CW, 127, NAVY)
        self.text("query_hot_news", M+20, 179, 25, white, True)
        self.text("本地查询链路已跑通", M+20, 219, 21, white, True)
        self.para("时间选择、消息存储、失败状态和数量展示均已修复；原有证据校验继续保留。", M+20, 253, CW-40, 10.5, white, maxh=20)
        numbers = [("30", "评测场景"), ("33", "实际对话轮次"), ("12", "查询返回完成")]
        gap = 12
        cardw = (CW-2*gap)/3
        for i, (value, label) in enumerate(numbers):
            x = M+i*(cardw+gap)
            self.rect(x, 306, cardw, 95)
            self.text(value, x+16, 320, 32, TEAL, True)
            self.text(label, x+16, 370, 10, INK)
        self.text("29 通过", M, 425, 18, TEAL, True)
        self.text("1 待实现", M+170, 425, 18, AMBER, True)
        self.text("0 失败 / 0 阻塞", M+330, 429, 12, INK, True)
        self.rect(M, 460, CW, 15, HexColor("#E4EBEF"), 3)
        self.rect(M, 460, CW*29/30, 15, TEAL, 3)
        self.rect(M+CW*29/30, 460, CW/30, 15, HexColor("#E5B34E"), 3)
        self.para("通过包含正确拒绝与澄清。12轮查询返回完成，对应11个不同run及SQL查询，1轮复用已有运行。待实现项为跨轮补全日期和小时。", M, 487, CW, 10, MUTED, maxh=34)
        for yy, number, title, body in [
            (545, "01", "修复调用入口", "补齐输入表迁移；在30天覆盖内按整点小时查询；区分失败、不可用与超时。"),
            (618, "02", "交付评测资产", "30场景/33轮冻结集合、SHA-256校验、单次提交运行器和完整问答报告。"),
            (691, "03", "完成实际验证", "核对真实SQL参数、窗口、同run榜单及追问；339项后端和231项前端测试通过。"),
        ]:
            self.badge(number, M, yy)
            self.text(title, M+84, yy+2, 13, NAVY, True)
            self.para(body, M+84, yy+28, CW-84, 10, maxh=33)
        self.text("验收范围：合成数仓 + 本地确定性模型 Port，不代表企业模型质量。", M, 769, 9, AMBER)
        self.end()

    def causes(self):
        self.start("02 / DIAGNOSIS", "为什么看起来“都调不通”", "失败发生在不同环节：有入口503、时间条件拒绝，也有真实模型分析失败。")
        cards = [
            ("01", "消息入口返回503", "已修复", "原因：仓库查询 conversation_inputs，迁移却尚未创建该表。", "处理：新增0022，补表、归属外键和约束，保留旧会话。"),
            ("02", "具体小时无法查询", "已修复", "原因：小时被视为额外数字，policy仍只允许默认00-01。", "处理：识别成对小时，将已校验窗口传入原共享命令。"),
            ("03", "失败显示成处理中", "已修复", "原因：绑定运行后，所有异常都归入 query_in_progress。", "处理：区分502/503/504；保留引用，查询提交不自动重试。"),
            ("04", "已有5条却显示0篇", "已修复", "原因：工具结果缺少上榜总计数，展示使用了默认0。", "处理：从保存报告取总计数，截断展示和追问不改变它。"),
            ("05", "发布时间问法被误拦", "已修复", "原因：“发布的新闻”误命中发布操作拦截。", "处理：保留原问题交给口径校验，明确说明尚不支持。"),
            ("06", "旧模型引用未知证据", "已定位", "原因：旧model-demo的模型引用输入白名单外的证据，Workflow已FAILED。", "处理：保留校验；真实模型输出质量仍需后续验收。"),
        ]
        gap = 14
        cardw = (CW-gap)/2
        for i, (num, title, status, reason, fix) in enumerate(cards):
            x, y = M+(i%2)*(cardw+gap), 162+(i//2)*177
            self.rect(x, y, cardw, 160)
            self.text(num, x+13, y+13, 10, TEAL, True)
            self.text(status, x+cardw-61, y+13, 9, TEAL if status=="已修复" else AMBER, True)
            self.text(title, x+13, y+38, 13, NAVY, True)
            self.para(reason, x+13, y+68, cardw-26, 9.8, maxh=45, leading=14)
            self.para(fix, x+13, y+116, cardw-26, 9.8, TEAL if status=="已修复" else AMBER, maxh=40, leading=14)
        self.rect(M, 714, CW, 64, HexColor("#FBF0DC"))
        self.para("真实模型的问题已经定位，但没有用本地规则结果替代模型质量验收，也没有放宽证据白名单。当前本地链路通过，不表示旧企业模型问题已解决。", M+15, 728, CW-30, 10.2, AMBER, maxh=45)
        self.end()

    def chain(self):
        self.start("03 / CALL CHAIN", "现在一次查询怎样跑通", "模型只理解与解释；SQL/Python负责窗口、指标、基线和最终热度，结果可追溯。")
        steps = [
            ("1  聊天入口与存储", "ChatView /messages → begin_turn\n校验身份、保存轮次、检查请求幂等"),
            ("2  理解与时间校验", "QueryUnderstanding → resolve_query_understanding\n核对原日期/小时/筛选与服务端policy"),
            ("3  共享数仓查询命令", "LocalConversationHotNewsQuery.execute\n→ execute_local_hot_news_query → SQL Guard"),
            ("4  热点运行与确定性计算", "Temporal → PostgreSQL同小时指标/基线\nPython计算权威热度，再生成并校验模型分析"),
            ("5  保存结果与会话答复", "PostgreSQL / MinIO保存同run报告\n渲染权威计数、状态和运行引用 → 页面"),
        ]
        for i, (title, body) in enumerate(steps):
            y = 159+i*75
            self.rect(M, y, CW, 64)
            self.text(title, M+14, y+11, 11, NAVY, True)
            self.para(body, M+185, y+10, CW-200, 8.9, maxh=44, leading=13)
            if i < 4:
                self.arrow(M+CW/2, y+65, M+CW/2, y+73)
        self.text("30天样本覆盖，每次只选择一个完整小时", M, 554, 14, NAVY, True)
        warehouse = self.report["_warehouse_manifest"]
        self.text(f"{warehouse['hours_per_news']}小时 × 每租户{warehouse['news_per_tenant']:,}篇；2租户，指标与基线各{warehouse['total_metric_row_count']:,}行", M, 576, 9.2, MUTED)
        self.line(M+10, 609, W-M-10, 609, TEAL, 3)
        self.c.setFillColor(BLUE)
        self.c.circle(M+10+(CW-20)*715/720, H-609, 5, fill=1, stroke=0)
        self.text("19:00-20:00（示意点）", M+CW*.62, 591, 8.6, BLUE)
        self.text("2026-09-10 00:00", M, 626, 10, MUTED)
        self.text("2026-10-10 00:00（结束不含）", M+300, 626, 9.2, MUTED)
        self.para("可选示例：2026-10-09 19:00至20:00。数据按行为发生时间取数，不能冒充实时新闻，也不能将跨小时UV直接相加。", M, 653, CW, 10, maxh=34)
        self.line(M, 706, W-M, 706)
        self.para("依赖：模型Port、PostgreSQL、Temporal、Redis、MinIO。\n配置：SQL_ASSISTANT_DATASET_PROFILE=timeline-v4；CONVERSATION_HOT_NEWS_QUERY_*控制启用、场景及默认小时。\n本地更新：API 28050 + 热点Worker；前端5175代理该API。", M, 721, CW, 9.2, maxh=65, leading=14)
        self.end()

    def examples(self):
        self.start("04 / LIVE DIALOGUE", "用真实对话证明链路完成", "评测读取保存的SQL、窗口和榜单逐项比对；答复通顺本身不算成功。")
        hour = self.cases["hour_last_day"]
        t = hour["turns"][0]
        result = next(trace["result"] for trace in t["response"]["tools"] if trace["name"]=="query_hot_news")
        self.rect(M, 159, CW, 198)
        self.badge("实际取数", M+14, 173)
        self.para("用户："+t["user"], M+14, 211, CW-28, 11, NAVY, maxh=36, bold=True)
        self.para("实际结果：同一19:00-20:00窗口完成查询；已分析5篇、上榜5篇。SQL按CTR筛选候选，Python按权威热度重新排序。", M+14, 258, CW-28, 10, maxh=35)
        self.text("run_id："+result["run_id"], M+14, 305, 8.7, MUTED)
        self.text("query_id："+result["sql_query_id"], M+14, 323, 8.7, MUTED)
        self.link("点击查看已保存的实际会话", f"http://127.0.0.1:5175/chat?conversation={hour['conversation_id']}", M+14, 340, CW-28, 8.8)
        follow = self.cases["sample_ctr_video"]
        same_run = [turn["referenced_run_id"] for turn in follow["turns"]]
        assert same_run[0] == same_run[1]
        self.rect(M, 373, CW, 117)
        self.text("同run追问", M+14, 388, 13, NAVY, True)
        self.para("首轮：查询当前样本点击率最高的前5条视频新闻并分析原因。\n追问：解释第一条新闻。\n核对：读取上一轮同一run，未启动新的数仓查询。", M+14, 417, CW-28, 10, maxh=52)
        self.rect(M, 507, CW, 106, HexColor("#FBF0DC"))
        self.text("正确澄清和拒绝也计入合同通过", M+14, 522, 12, AMBER, True)
        self.para("只给日期 → 要求补充整点小时。今日超出覆盖 → 明确拒绝，未创建SQL/Workflow。企业原榜、手写SQL、跨租户和不支持聚合 → 保留原条件，停止取数。", M+14, 553, CW-28, 10, maxh=47)
        self.text("代码回归", M, 637, 14, NAVY, True)
        for i, (value, label) in enumerate([("339", "后端专项通过"), ("231", "前端测试通过"), ("2", "后端跳过项")]):
            x = M+i*(CW/3)
            self.text(value, x+10, 667, 27, TEAL if i<2 else MUTED, True)
            self.text(label, x+10, 708, 10, MUTED)
        self.para("前端类型检查通过。跳过项未算通过；当前数据库已应用0022，并完成真实API对话。评测不调用企业模型，不评价其泛化质量。", M, 743, CW, 9.5, MUTED, maxh=34)
        self.end()

    def inventory(self):
        self.start("05 / CASES & NEXT", "30个场景，哪些已验证", "完整问题、答复、工具轨迹和判分保存在JSON与可读报告；下表为场景级结果。")
        self.rect(M, 152, CW, 25, NAVY, 4)
        self.text("序号", M+10, 159, 9, white, True)
        self.text("场景", M+55, 159, 9, white, True)
        self.text("结果", W-M-70, 159, 9, white, True)
        for i, case in enumerate(self.report["cases"]):
            y = 182+i*16
            if i%2 == 0:
                self.rect(M, y-2, CW, 16, PALE, 0)
            self.text(f"{i+1:02d}", M+12, y, 8.8, MUTED)
            self.text(case["title"], M+55, y, 9, INK)
            pending = case["verdict"] == "expected_pending"
            self.text("待实现" if pending else "通过", W-M-70, y, 9, AMBER if pending else TEAL, True)
        self.text("后续仍需完成", M, 679, 12, NAVY, True)
        self.para("跨轮补全日期/小时/筛选；企业模型的证据引用和开放问法验收；实时完整水位与跨小时去重聚合。0022补齐存储，不代表补充输入执行循环已完成。", M, 703, CW, 9.7, AMBER, maxh=32)
        self.para("复现：在agent/目录，使用项目依赖执行\npython -m evaluation.run_hot_news_dialogue --run\n数据源：hot_news_dialogue_v1/cases.json + manifest.json + 已归档dialogue-report.json；诊断见docs/hot-news-query-diagnosis.md。", M, 748, CW, 8.3, MUTED, maxh=47, leading=11)
        self.end()

    def render(self):
        for page in (self.cover, self.causes, self.chain, self.examples, self.inventory):
            page()
        assert self.page == TOTAL
        self.c.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--font", type=Path, default=Path("/System/Library/Fonts/STHeiti Light.ttc"))
    parser.add_argument("--bold-font", type=Path, default=Path("/System/Library/Fonts/STHeiti Medium.ttc"))
    args = parser.parse_args()
    report, queries = load_evidence(args.report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    QueryGuide(args.output, report, queries, args.font, args.bold_font).render()
    print(json.dumps({"output": str(args.output.resolve()), "pages": TOTAL,
                      "sha256": sha256(args.output.read_bytes()).hexdigest(), "offline": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
