"""Offline PDF guide for NewsAgent evaluation and future training data.

No Settings instances, dotenv, network, database, inference or training calls.
CLI -> build_examples -> existing schema checks -> Guide -> ReportLab PDF.
Run with the bundled Python runtime; only reportlab and pydantic are required.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from html import escape
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
OUTPUT = ROOT / "output/pdf/NewsAgent_评测与训练数据规范_2026-10-13.pdf"
FONT = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
BOLD_FONT = Path("/System/Library/Fonts/STHeiti Light.ttc")
W, H = A4
M = 40
CW = W - 2 * M
INK = HexColor("#25384A")
NAVY = HexColor("#12314A")
TEAL = HexColor("#00818A")
BLUE = HexColor("#326BBA")
GRAY = HexColor("#667785")
PALE = HexColor("#F0F5F8")
LINE = HexColor("#D9E3EB")
AMBER = HexColor("#9B651D")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def compact_lines(value):
    """Compact homogeneous scalar containers; keep all JSON values intact."""
    def render(obj, level=0):
        pad = "  " * level
        if isinstance(obj, dict):
            if obj and all(not isinstance(v, (dict, list)) for v in obj.values()):
                s = json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))
                if len(s) < 105:
                    return s
            parts = ["{" ]
            for i, (key, item) in enumerate(obj.items()):
                rendered = render(item, level + 1)
                parts.append("  " * (level + 1) + json.dumps(key, ensure_ascii=False)
                             + ": " + rendered + ("," if i < len(obj) - 1 else ""))
            return "\n".join(parts) + "\n" + pad + "}"
        if isinstance(obj, list):
            if all(not isinstance(v, (dict, list)) for v in obj):
                return json.dumps(obj, ensure_ascii=False)
            return "[\n" + ",\n".join("  " * (level + 1) + render(v, level + 1)
                                         for v in obj) + "\n" + pad + "]"
        return json.dumps(obj, ensure_ascii=False)
    rendered = render(value)
    assert json.loads(rendered) == value
    return rendered


@dataclass
class Page:
    title: str
    subtitle: str
    blocks: list


class Guide:
    def __init__(self, output, font, bold_font, total):
        pdfmetrics.registerFont(TTFont("CN", str(font), subfontIndex=0))
        pdfmetrics.registerFont(TTFont("CNBold", str(bold_font), subfontIndex=0))
        self.c = canvas.Canvas(str(output), pagesize=A4, pageCompression=1)
        self.c.setTitle("NewsAgent 评测与训练数据规范")
        self.c.setAuthor("NewsAgent")
        self.c.setSubject("数据清单、JSON格式、评测、SFT准备、DPO与RL预留、项目调用链")
        self.total = total
        self.number = 0
        self.y = 0
        self.bounds = []

    def paragraph(self, value, x, top, width, size=10.3, color=INK,
                  bold=False, leading=None):
        style = ParagraphStyle("text", fontName="CNBold" if bold else "CN",
            fontSize=size, leading=leading or size * 1.5,
            textColor=color, wordWrap="CJK", splitLongWords=1)
        p = Paragraph(escape(str(value)).replace("\n", "<br/>"), style)
        _, height = p.wrap(width, H)
        if top + height > H - 55:
            raise ValueError(f"Page {self.number} overflow: {str(value)[:80]}")
        p.drawOn(self.c, x, H - top - height)
        self.bounds.append((self.number, top, top + height))
        return height

    def p(self, value, size=10.3, color=INK):
        self.y += self.paragraph(value, M, self.y, CW, size, color) + 11

    def h2(self, value):
        self.y += 4
        self.y += self.paragraph(value, M, self.y, CW, 12.3, TEAL, True) + 9

    def note(self, value, color=TEAL):
        style = ParagraphStyle("note", fontName="CN", fontSize=9.8,
                               leading=14.5, textColor=color, wordWrap="CJK")
        p = Paragraph(escape(value).replace("\n", "<br/>"), style)
        _, height = p.wrap(CW - 28, H)
        height += 23
        if self.y + height > H - 55:
            raise ValueError(f"Page {self.number} note overflow")
        self.c.setFillColor(PALE)
        self.c.roundRect(M, H - self.y - height, CW, height, 7, fill=1, stroke=0)
        self.c.setFillColor(color)
        self.c.rect(M, H - self.y - height, 3, height, fill=1, stroke=0)
        p.drawOn(self.c, M + 14, H - self.y - 11 - (height - 23))
        self.y += height + 12

    def wrapped_code(self, line, size, width):
        if pdfmetrics.stringWidth(line, "CN", size) <= width:
            return [line]
        initial = len(line) - len(line.lstrip())
        result, remainder = [], line
        while pdfmetrics.stringWidth(remainder, "CN", size) > width:
            limit = 1
            while limit < len(remainder) and pdfmetrics.stringWidth(remainder[:limit + 1], "CN", size) <= width:
                limit += 1
            result.append(remainder[:limit])
            remainder = " " * min(initial + 2, 8) + remainder[limit:]
        result.append(remainder)
        return result

    def code(self, value, size=8.7, leading=11.8):
        lines = []
        for line in str(value).splitlines():
            lines.extend(self.wrapped_code(line, size, CW - 26))
        height = len(lines) * leading + 22
        if self.y + height > H - 55:
            raise ValueError(f"Page {self.number} code overflow: {len(lines)} lines, y={self.y}")
        self.c.setFillColor(PALE)
        self.c.roundRect(M, H - self.y - height, CW, height, 7, fill=1, stroke=0)
        self.c.setFillColor(INK)
        self.c.setFont("CN", size)
        for i, line in enumerate(lines):
            self.c.drawString(M + 13, H - self.y - 15 - i * leading, line)
        self.y += height + 12

    def table(self, headers, rows, widths=None, size=9.2):
        widths = widths or [CW / len(headers)] * len(headers)
        def cell(text, width, is_header=False):
            style = ParagraphStyle("cell", fontName="CNBold" if is_header else "CN",
                fontSize=size, leading=size * 1.43, wordWrap="CJK",
                textColor=white if is_header else INK)
            p = Paragraph(escape(str(text)).replace("\n", "<br/>"), style)
            _, h = p.wrap(width - 16, H)
            return p, h
        for idx, row in enumerate([headers] + rows):
            cells = [cell(text, width, idx == 0) for text, width in zip(row, widths, strict=True)]
            height = max(h for _, h in cells) + 17
            if self.y + height > H - 55:
                raise ValueError(f"Page {self.number} table overflow row {idx}")
            self.c.setFillColor(NAVY if idx == 0 else (PALE if idx % 2 else white))
            self.c.rect(M, H - self.y - height, CW, height, fill=1, stroke=0)
            x = M
            for (p, h), width in zip(cells, widths, strict=True):
                p.drawOn(self.c, x + 8, H - self.y - 8 - h)
                x += width
            self.y += height
        self.y += 13

    def flow(self, rows):
        for i, row in enumerate(rows):
            self.note(row, BLUE if i in (2, 3) else TEAL)

    def page(self, spec):
        self.number += 1
        self.c.bookmarkPage(f"p{self.number}")
        self.c.addOutlineEntry(spec.title, f"p{self.number}", level=0)
        self.paragraph("NEWSAGENT / DATA SPECIFICATION / 2026-10-13", M, 27, CW, 8.3, TEAL, True)
        self.paragraph(spec.title, M, 57, CW, 21.5, NAVY, True)
        self.paragraph(spec.subtitle, M, 99, CW, 10, GRAY)
        self.y = 145
        for block in spec.blocks:
            getattr(self, block[0])(*block[1:])
        self.c.setStrokeColor(LINE)
        self.c.line(M, 40, W - M, 40)
        self.c.setFillColor(GRAY)
        self.c.setFont("CN", 8)
        self.c.drawString(M, 25, "NewsAgent 数据规范 v1 · 现有接口 + 拟新增采集格式 · 所有样例均为虚构")
        self.c.drawRightString(W - M, 25, f"{self.number:02d} / {self.total:02d}")
        self.c.showPage()

    def finish(self):
        assert self.number == self.total
        self.c.save()


def build_examples():
    source_id = "demo-bus-plan-001"
    inp = {
        "news_id": "demo-bus-001",
        "title": "虚构示例：青禾市社区接驳线路开始试运行",
        "summary": "交通服务中心公布停靠站点与服务时段，效果待评估。",
        "content_excerpt": "虚构青禾市9时20分公告：线路9时30分试运行，尚无客流与满意度结果。",
        "content_type": "article",
        "window_start": "2026-10-12T09:00:00+08:00",
        "window_end": "2026-10-12T10:00:00+08:00",
        "hot_score": 0.4225,
        "metrics": {"impressions": 1000, "clicks": 400, "ctr": 0.4,
                    "unique_users": 700, "effective_consumptions": 300, "interactions": 60},
        "score_components": {"click": 0.12, "consumption": 0.2625,
                             "interaction": 0.04, "growth": 0.0},
        "related_news": [{"news_id": source_id,
                          "title": "虚构示例：社区接驳试运行安排",
                          "excerpt": "9时10分公告列明站点和时段，客流结果待后续评估。",
                          "source_url": "https://example.com/demo/bus-plan-001",
                          "published_at": "2026-10-12T09:10:00+08:00",
                          "final_score": 0.9,
                          "rerank_reasons": ["同一虚构事件", "补充试运行范围"]}],
        "analysis_policy_version": "hot-news-analysis-v1",
        "memory_context": None,
    }
    target = {
        "news_id": "demo-bus-001",
        "trend_assessment": "点击率40%，有效消费300次；消费分量最高。缺历史基线，不能判断关注上升。",
        "dominant_driver": "consumption",
        "attention_reasons": [
            {"reason_type": "metric", "statement": "有效消费占点击75%，消费分量为0.2625。",
             "metric_keys": ["effective_consumptions", "clicks", "consumption_component"],
             "evidence_news_ids": [], "confidence": 1.0, "certainty": "observed"},
            {"reason_type": "hypothesis", "statement": "实用信息可能与阅读有关，但无法验证因果。",
             "metric_keys": ["effective_consumptions"],
             "evidence_news_ids": [source_id], "confidence": 0.45, "certainty": "inferred"}],
        "related_contexts": [{"statement": "线路仍在试运行，客流效果待评估。",
                              "evidence_news_ids": [source_id], "confidence": 0.95}],
        "operation_suggestions": [{"action": "建议运营审核后补充站点和服务时段。",
                                   "rationale": "提供实用信息，避免把试运行写成效果获证实。",
                                   "priority": "medium", "metric_keys": ["consumption_component"],
                                   "evidence_news_ids": [source_id]}],
        "evidence_news_ids": [source_id], "applied_memory_ids": [],
        "limitations": ["全部内容与数字均为虚构示例；链接为占位地址。",
                        "无历史基线；增长分量为0不表示没有增长。",
                        "单小时聚合不能证明线路运营效果。"],
        "overall_confidence": 0.85, "output_schema_version": "1.0",
    }
    expected = {"allowed_dominant_drivers": ["consumption"],
                "required_evidence_news_ids": [source_id],
                "forbidden_evidence_news_ids": ["demo-other-city-001"],
                "required_metric_keys": ["effective_consumptions", "consumption_component"],
                "must_state_limitation": True}
    evaluation = {"dataset_name": "hot-news-format-demo", "dataset_version": "2026-10-13.demo-v1",
                  "schema_version": "1.0", "description": "虚构格式样例，不是正式人工批准数据集。",
                  "cases": [{"case_id": "demo-eval-001", "description": "消费主驱动；无历史基线。",
                             "tags": ["synthetic", "no-baseline"],
                             "analysis_input": inp, "expected": expected}]}
    meta = {"schema_version": "newsagent-data-ledger-v1-proposed",
            "sample_id": "demo-sample-001", "task_type": "hot_news_analysis",
            "data_kind": "synthetic", "scope": {"tenant_ref": "demo-tenant-A", "owner_ref": None},
            "event_group_id": "demo-event-bus-001", "conversation_group_id": None,
            "near_duplicate_group_id": "demo-duplicate-bus-001",
            "observed_at": "2026-10-12T10:00:00+08:00",
            "split": "development", "split_version": "demo-split-v1",
            "allowed_uses": ["format_validation"], "redaction_version": "demo-redaction-v1",
            "source_case_ref": None, "retention_until": "2027-01-13T00:00:00+08:00",
            "annotation": {"status": "pending", "guideline_version": "demo-guide-v1",
                           "labeled_by": None, "reviewed_by": None},
            "input_sha256": digest(inp), "candidate_target_sha256": digest(target)}
    # This educational pair is a future preference-format demonstration only.
    dpo = {"prompt": [{"role": "system", "content": "仅根据给定聚合和事实解释，明确数据局限。"},
                      {"role": "user", "content": "点击400，曝光1000；无历史基线。试运行效果待评估。请解释。"}],
           "chosen": [{"role": "assistant", "content": "点击率为40%。缺少历史基线，不能判断关注上升；试运行效果尚待评估。"}],
           "rejected": [{"role": "assistant", "content": "关注大幅上升，接驳线路成功改善了出行，建议立即推送。"}]}
    return {"input": inp, "target": target, "expected": expected,
            "evaluation": evaluation, "metadata": meta, "dpo": dpo}


def validate_examples(ex):
    from app.modules.hot_news.contracts.hot_news import HotNewsAnalysisInput, HotNewsAnalysisReport
    from app.model_runtime.core import PromptRegistry, PromptSpec, StructuredInferenceService
    inp = HotNewsAnalysisInput.model_validate(ex["input"])
    report = HotNewsAnalysisReport.model_validate(ex["target"])
    allowed_ids = {item.news_id for item in inp.related_news}
    assert report.news_id == inp.news_id
    assert set(report.evidence_news_ids) <= allowed_ids
    assert all(set(reason.evidence_news_ids) <= allowed_ids for reason in report.attention_reasons)
    assert all(reason.certainty == "inferred" for reason in report.attention_reasons
               if reason.reason_type == "hypothesis")
    assert Decimal(str(inp.metrics.ctr)) == Decimal(inp.metrics.clicks) / Decimal(inp.metrics.impressions)
    components = inp.score_components
    assert Decimal(str(inp.hot_score)) == sum(Decimal(str(v)) for v in components.model_dump().values())
    assert Decimal(str(components.click)) == Decimal(400) / Decimal(1000) * Decimal("0.30")
    assert Decimal(str(components.consumption)) == Decimal(300) / Decimal(400) * Decimal("0.35")
    assert Decimal(str(components.interaction)) == Decimal(60) / Decimal(300) * Decimal("0.20")
    assert components.growth == 0
    assert all(item.published_at <= inp.window_end for item in inp.related_news)
    assert inp.window_end > inp.window_start
    class NoInference:
        async def complete(self, request):
            raise AssertionError("This guide must never call a model")
    service = StructuredInferenceService(inference=NoInference(), prompts=PromptRegistry([
        PromptSpec(scene="hot_news_analysis", version="pdf-format-demo-v1",
                   system_prompt="根据给定指标与新闻证据生成分析，区分事实与推断。")]))
    rendered = service.render_input(scene="hot_news_analysis", prompt_version="pdf-format-demo-v1",
        payload=inp, output_type=HotNewsAnalysisReport)
    ex["sft_rendered"] = {"prompt": [{"role": m.role, "content": m.content} for m in rendered.messages],
                          "completion": [{"role": "assistant", "content": report.model_dump_json()}]}
    assert json.loads(rendered.messages[1].content) == inp.model_dump(mode="json")
    assert "JSON Schema" in rendered.messages[0].content
    assert rendered.output_json_schema == report.model_json_schema()
    return {"existing_input_schema": "passed", "existing_report_schema": "passed",
            "reference_and_certainty_checks": "passed", "ctr_and_components": "passed",
            "evidence_cutoff": "passed", "runtime_rendered_input": "passed",
            "human_review": "pending", "enterprise_model_quality": "not_evaluated"}


def validate_business(ex, python):
    code = """
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'agent'))
from app.modules.hot_news.contracts.hot_news import HotNewsAnalysisInput, HotNewsAnalysisReport
from app.model_runtime.result import AgentResult
from app.modules.hot_news.analysis.hot_news_analysis import HotNewsAnalysisValidator
from app.modules.data_loop.evaluation.hot_news import HotNewsEvaluationDataset
data = json.load(sys.stdin)
inp = HotNewsAnalysisInput.model_validate(data['input'])
out = HotNewsAnalysisReport.model_validate(data['target'])
HotNewsAnalysisValidator().validate(analysis_input=inp, result=AgentResult(
    value=out, request_id='offline-format-check', usage={}, raw_content=out.model_dump_json()))
HotNewsEvaluationDataset.model_validate(data['evaluation'])
print('passed')
"""
    result = subprocess.run([str(python), "-c", code], cwd=ROOT,
        input=json.dumps({k: ex[k] for k in ("input", "target", "evaluation")}),
        text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "passed"
    return "passed"


def build_pages(ex, validation):
    pages = []
    pages.append(Page("评测与训练数据：需要什么、怎么交付", "适用前提：企业当前仅提供推理和 Embedding 接口；优先建设评测及训练数据准备。", [
        ("note", "先准备能重放、能判断、能追溯的样本。现在就可开展真实模型评测和 Prompt 实验；SFT/DPO/RL 的实际训练还需要独立训练服务或获准权重与计算环境。"),
        ("h2", "第一批准备五类数据"),
        ("table", ["数据", "用途", "提供者"], [
            ["新闻与证据快照", "保留当时可见事实、来源与版本", "内容/知识库团队"],
            ["有界窗口聚合指标", "复算数字、热度及数据完整性", "企业数仓 + Python"],
            ["脱敏问题与运行结果", "复现模型及工具错误", "项目自动采集"],
            ["人工标签与完整修订答案", "正式评测；未来 SFT", "运营/编辑 + 独立复核人"],
            ["用途、分组、版本与哈希清单", "防泄漏、审核、冻结及追溯", "数据负责人"],
        ], [135, 245, CW - 380]),
        ("h2", "先读这些页面"),
        ("p", "第2-3页：采集清单与字段规范；第4-6页：完整热点输入、正确答案与现有评测格式；第7-8页：意图、对话轨迹与检索评测；第9-11页：SFT、DPO 与 RL 数据；第12-15页：划分、质量检查、项目接入和依据。"),
        ("h2", "文中的三个状态"),
        ("p", "现有：当前项目 Schema 或服务已支持。拟新增：建议采集/训练契约，当前 API 尚不接受。格式示例：虚构教学数据，审核状态始终 pending，不能冒充真实批准数据或企业效果。"),
        ("note", "交付采用 UTF-8 JSONL + manifest.json + 标注规范。业务人员可先用表格标注，导入时再做强类型转换。所有示例的数字为合成聚合值，未收集企业原始用户行为。", AMBER),
    ]))
    pages.append(Page("采集清单：由谁提供，收集到什么粒度", "按目标任务收集必要字段，无需把所有历史数据或用户行为明细搬进项目。", [
        ("table", ["优先级 / 数据", "最低字段与粒度", "用途 / 格式"], [
            ["P0 内容与证据", "news_id；标题、必要正文片段；栏目/类型；来源；发布时间、更新时间、内容版本；使用授权", "重放事实输入\nJSONL / 受控内容引用"],
            ["P0 聚合指标", "news_id + tenant_ref + [start,end)；曝光/点击/去重UV/有效消费/互动；watermark；口径与快照版本", "复算指标；检查覆盖\nJSONL / CSV"],
            ["P0 基线与热度", "可比较基线或显式null；基线窗口/版本；四分量与总分；规则版本", "解释主驱动及限制\nJSON对象"],
            ["P0 推理记录", "脱敏用户问题、必要历史、模型输入/原输出；Prompt/模型/Schema版本；状态/错误码/耗时", "真实评测与错误归因\nJSONL"],
            ["P0 人工评测", "对/错/部分错/不可评；必需/禁止证据；指标；允许主驱动；限制；二审与规范版本", "正式评测标签\n标注表转JSONL"],
            ["P1 完整修订答案", "人工修订后的完整输出，绑定同一输入、证据和Schema；独立复核", "未来SFT；不是原错误输出\nJSON / JSONL"],
            ["P1 检索/对话记录", "冻结语料与候选池、相关性标签；工具白名单、参数、结果快照、完整性", "检索/完整Agent评测\nJSONL + 快照引用"],
            ["P2 偏好/奖励", "同输入回答对、偏好理由；终态/奖励分量/环境版本；明确用途授权", "未来DPO/RL\n待审批契约"],
        ], [110, 270, CW - 380], 9),
        ("note", "模型平台另外提供：批准路由和版本、上下文/超时/限流合同、Embedding版本与维度、数据保留及训练使用限制。配置仅保存密钥引用名称，样本不得包含密钥或身份头。", AMBER),
    ]))
    pages.append(Page("统一样本台账：业务载荷与管理信息分开", "以下外壳为拟新增契约；不能原样提交到当前热点输入/评测 API。", [
        ("code", compact_lines(ex["metadata"]), 8.8, 11.9),
        ("p", "input_sha256 与 candidate_target_sha256 对本册第4、5页完整对象按排序键、紧凑UTF-8 JSON计算；哈希用于完整性检查，不等于人工批准或授权签名。", 9.5),
        ("table", ["字段约定", "含义与处理"], [
            ["ID与scope", "sample_id唯一；scope由服务端绑定。真实身份映射留在企业授权系统；化名仍需治理。"],
            ["split与allowed_uses", "development用于格式开发；正式拆train/validation/test。获准用途不足则排除训练。"],
            ["annotation", "pending/review_pending/approved/conflict/not_evaluable 为拟定状态；二审人与标注人不同。"],
        ], [145, CW - 145], 9),
        ("note", "正文input、expected、candidate_target单独存放并按sample_id/内容哈希关联。现有Schema使用extra=forbid，禁止把split、审核人或tenant_ref塞进HotNewsAnalysisInput。", AMBER),
    ]))
    pages.append(Page("热点分析输入：完整现有 Schema 示例", "此对象可按 HotNewsAnalysisInput 做结构校验；新闻、指标及链接全部为虚构。", [
        ("code", compact_lines(ex["input"]), 8.6, 11.7),
        ("p", "窗口为北京时间09:00至10:00，结束不含；证据在观察时点前发布。ctr用0.4表示40%，绝不把40直接填入。unique_users为企业在这个完整窗口内去重后的700，不能跨小时相加。", 9.3),
        ("note", "Python默认公式：点击0.4×0.30=0.12；消费(300/400)×0.35=0.2625；互动(60/300)×0.20=0.04；无基线增长为0；总热度0.4225。指标与分量来自确定性层，不由模型生成。"),
    ]))
    pages.append(Page("完整目标答案：未来 SFT 所需的新增数据", "内层兼容现有 HotNewsAnalysisReport；候选答案通过结构校验，人工复核仍为 pending。", [
        ("code", compact_lines(ex["target"]), 8.5, 11.5),
        ("note", "reason_type=hypothesis必须certainty=inferred。applied_memory_ids为必填，未用Memory时填[]。正确引用ID只证明来源合法，还需要人工判断语义是否被证据支持。", AMBER),
    ]))
    pages.append(Page("现有评测格式：规则标签不是完整答案", "保留原输出用于归因，使用独立标签验收。当前离线热点 Schema 与正式冻结账本是两种入口。", [
        ("h2", "现有 ExpectedHotNewsAnalysis 的完整标签示例"),
        ("code", pretty(ex["expected"]), 9.1, 12.5),
        ("p", "禁止ID可以是没有出现在允许输入中的其他主体。允许多个合理主驱动；不要求模型逐字复制标准文章。正式Feedback标签还需verdict及operator_comment，并完成独立二审。", 9.7),
        ("h2", "组装现有离线评测数据集：完整结构"),
        ("code", 'dataset = {\n  "dataset_name": "hot-news-format-demo",\n  "dataset_version": "2026-10-13.demo-v1",\n  "schema_version": "1.0",\n  "description": "虚构格式样例，非正式批准数据。",\n  "cases": [{\n    "case_id": "demo-eval-001",\n    "description": "消费主驱动；无历史基线。",\n    "tags": ["synthetic", "no-baseline"],\n    "analysis_input": input_object,  # 第4页完整对象\n    "expected": expected_object     # 本页完整对象\n  }]\n}', 8.9, 12),
        ("p", "上面是Python组装代码，不是JSON中的引用语法。序列化后analysis_input和expected必须为完整对象；当前加载器不支持$ref。正式冻结必须通过Feedback→标签二审→Freezer，不能用此demo绕过审批。", 9.5),
        ("note", "现有 observed_output 是原模型结果，可能正是错误答案。新增candidate_target经二审后才能转reviewed_target；不把标签字段直接当SFT completion。", AMBER),
    ]))
    return pages + build_remaining_pages(ex, validation)


def build_remaining_pages(ex, validation):
    query_example = {
        "sample_id": "demo-query-001", "task_type": "query_understanding",
        "question": "查2026年10月12日9点至10点点击率最高的前5条视频新闻。",
        "as_of": "2026-10-12T10:00:00+08:00", "timezone": "Asia/Shanghai",
        "policy_snapshot_ref": "demo-approved-hour-policy-v1",
        "expected_proposal": {"status": "ready", "intent": {
            "sort_by": "ctr", "sort_direction": "desc", "content_type": "video",
            "category": None, "row_limit": 5, "explanation": "按该小时视频新闻点击率降序选5条。"},
            "time_expression": "date", "explicit_date": "2026-10-12",
            "hour_start": 9, "hour_end": 10, "time_basis": "behavior",
            "ranking_source": "unspecified", "response_style": "list", "unsupported_conditions": []},
        "expected_behavior": {"must_match_requested_window": True,
                              "allow_raw_sql_from_model": False,
                              "followup_must_reuse_run": True},
        "annotation_status": "pending",
    }
    retrieval = {
        "sample_id": "demo-retrieval-001", "task_type": "retrieval",
        "input": {"query": "青禾市接驳试运行的范围与限制", "source_news_id": "demo-bus-001",
                  "as_of": "2026-10-12T10:00:00+08:00", "corpus_snapshot_id": "demo-corpus-v1",
                  "filters": {"tenant_ref": "demo-tenant-A"},
                  "exclude_news_ids": ["demo-bus-001"], "top_k": 5},
        "expected": {"judgment_unit": "news", "relevance_scale_version": "demo-0-3-v1",
                     "qrels": [{"news_id": "demo-bus-plan-001", "content_version": 1, "grade": 3},
                               {"news_id": "demo-other-city-001", "content_version": 1, "grade": 0}],
                     "judged_pool_ref": "demo-pool-001", "no_relevant_result": False,
                     "forbidden_news_ids": ["demo-other-tenant-001"]},
        "observed": None, "annotation_status": "pending",
    }
    rl = {
        "sample_id": "demo-trajectory-001", "data_kind": "synthetic",
        "task_type": "dialogue_tool_use", "environment_version": "demo-env-v1",
        "trajectory_ref": "demo-dialogue-001", "prompt_snapshot_ref": "demo-turn-input-001",
        "terminal_status": "not_executed",
        "metrics": {"task_success": None, "tool_call_count": None,
                    "elapsed_ms": None, "token_usage": None},
        "reward": {"status": "not_computed", "policy_version": None,
                   "hard_failures": [], "components": None, "total": None},
        "allowed_uses": ["format_validation"], "annotation_status": "pending",
    }
    payload_files = {"hot_news_input.json": ex["input"], "candidate_target.json": ex["target"]}
    manifest = {"schema_version": "newsagent-data-manifest-v1-proposed",
                "dataset_name": "newsagent-format-demo", "dataset_version": "2026-10-13.demo-v1",
                "purpose": "format_validation", "data_kind": "synthetic",
                "source_cutoff_at": "2026-10-12T10:00:00+08:00",
                "case_count": 1, "independent_event_count": 1, "approved_case_count": 0,
                "split_version": "demo-split-v1", "guideline_version": "demo-guide-v1",
                "redaction_version": "demo-redaction-v1",
                "serialization": "UTF-8; json.dumps(indent=2,ensure_ascii=False) + LF",
                "files": [{"path": name, "file_sha256": sha256((pretty(value) + "\n").encode()).hexdigest()}
                          for name, value in payload_files.items()]}
    exporter = '''import json
from app.modules.hot_news.contracts.hot_news import HotNewsAnalysisInput, HotNewsAnalysisReport

# 拟新增导出函数；service注入获准的PromptRegistry与模型Port。
def to_sft_row(case, service, prompt_version):
    if case["annotation"]["status"] != "approved":
        raise ValueError("human review is required")
    if "sft_training" not in case["allowed_uses"]:
        raise ValueError("training use is not authorized")
    if case["split"] != "train":
        raise ValueError("held-out samples must not be exported")
    inp = HotNewsAnalysisInput.model_validate(case["input"])
    target = HotNewsAnalysisReport.model_validate(case["reviewed_target"])
    # 实际导出前还须经过业务Validator和完整性检查。
    rendered = service.render_input(
        scene="hot_news_analysis", prompt_version=prompt_version,
        payload=inp, output_type=HotNewsAnalysisReport,
    )
    prompt = [{"role": m.role, "content": m.content}
              for m in rendered.messages]
    completion = [{"role": "assistant", "content": target.model_dump_json()}]
    return {"prompt": prompt, "completion": completion}

# 每条样本独立序列化；下列参数由获准导出任务提供。
row = to_sft_row(approved_case, service, approved_prompt_version)
line = json.dumps(row, ensure_ascii=False, allow_nan=False) + "\\n"
# 如平台要求messages：{"messages": row["prompt"] + row["completion"]}'''
    pages = [
        Page("问句理解与对话：评测语义和执行行为", "外层为拟新增台账；expected_proposal字段对齐现有QueryUnderstanding。该小时覆盖需由测试policy明确提供。", [
            ("code", compact_lines(query_example), 8.6, 11.6),
            ("p", "模型只提出语义；服务端决定时间覆盖、权限、watermark与SQL编译。不能因为status=ready就认为查询一定可执行；现演示环境可能只覆盖另一日期。", 9.4),
            ("h2", "多轮工具记录：每一步保存这些字段"),
            ("p", "turn_index、脱敏user_text、冻结history_refs；step_id、tool_name、validated_arguments、status、attempts、error_code、elapsed_ms、result_snapshot_ref与哈希；request/run/query引用。实际ID只在执行后绑定，不能由模型编造。", 9.4),
            ("note", "第二轮“解释第一条”：预期复用首轮已完成run；验证结果引用与顺序，不要求唯一工具路径。保存完整可公开轨迹与真实工具结果，勿收集隐式思维链。未提交的中途轨迹须标incomplete，不能当完整任务成功样本。", AMBER),
        ]),
        Page("检索评测：问题、冻结语料和相关性标签", "下列为拟新增检索评测格式；当前热点related_news快照无法独自复现整个召回过程。", [
            ("code", compact_lines(retrieval), 8.9, 12),
            ("table", ["还需冻结", "用途"], [
                ["语料及候选池", "corpus文件哈希、文档/切片版本、已判断候选集合；避免把缺召回归因成模型生成错误。"],
                ["实际命中", "rank、news_id/chunk_id、原始/最终分数、Embedding/索引/重排版本与片段哈希。"],
                ["权限与截止时间", "查询租户/授权过滤、发布时间上限、源新闻排除；与线上相同。"],
            ], [130, CW - 130], 9.2),
            ("note", "建议相关性等级：0不相关，1弱背景，2相关，3直接支持；具体判断需人工规范。按news或chunk选定一种计分单位。未标注结果不自动当不相关；报告judged覆盖。Recall@K、nDCG@K、MRR之外，单独统计越权与未来证据。", AMBER),
        ]),
        Page("SFT：从审核答案导出训练平台格式", "现阶段准备数据；当前企业接口不能训练。下面是拟新增导出逻辑，未接入生产服务。", [
            ("p", "最终每行采用prompt/completion，或平台要求的messages。项目治理信息放独立台账；模型看不到split、审核结论、参考标签和未来结果。完整目标来自第5页同输入下的人工修订答案。", 9.7),
            ("code", exporter, 8.5, 11.8),
            ("note", "render_input必须复用拟上线的批准Prompt版本：system中包含完整输出JSON Schema，user中包含冻结输入。勿使用简化一句话Prompt代替真实线上输入。训练框架的chat template、tokenizer、截断与只对目标答案计loss的配置另行锁定。"),
            ("p", "本册虚构样例annotation=pending、用途仅format_validation，必须被正式SFT导出器拒绝。含私人memory_context的输入默认排除共享训练；需独立用途审批，或重新构造并复核移除私人上下文后的输入和目标。", 9.4, AMBER),
        ]),
        Page("DPO：同一输入下的偏好回答对", "可选未来数据。短答例仅演示通用偏好格式；生产热点分析应保留完整结构化Report。", [
            ("code", pretty(ex["dpo"]), 9.1, 12.5),
            ("h2", "偏好台账还要记录什么"),
            ("table", ["字段", "要求"], [
                ["prompt/input_sha256", "两份回答必须面对相同问题、证据、权限、工具结果和观察时点；不能拿不同新闻候选直接组成一对。"],
                ["preference_kind/reasons", "区分事实错误、因果过度、引用错误、编辑表达、个人风格和策略约束；tie/不可评单独保存。"],
                ["owner_ref/share_approval_ref", "个人偏好只在批准作用域内使用；不能自动晋升全租户标准或共享模型训练数据。"],
                ["review与用途授权", "人工选择并独立二审。例中chosen更谨慎，rejected把相关性写成因果且跳过运营审核；示例仍pending。"],
            ], [150, CW - 150], 9.2),
            ("note", "通用字段是prompt/chosen/rejected。实际热点导出使用第9页的同一render_input，content为完整Report JSON字符串；目标模型的格式由训练合同决定。先保障事实与边界，再评文风。", AMBER),
        ]),
        Page("RL：现在记录轨迹，未来再计算奖励", "以下为拟新增研究记录。奖励为空表示未执行，不是训练失败，也不能虚构奖励成绩。", [
            ("code", compact_lines(rl), 9, 12.1),
            ("h2", "未来可验证奖励的构成"),
            ("table", ["类型", "建议依据"], [
                ["硬失败", "越权、未来证据、伪造来源、数字篡改、绕过审批；上线继续由确定性护栏阻断，奖励不能赋予权限。"],
                ["任务质量", "正确意图/澄清、证据真正支持结论、完成真实工具任务；语义评审与独立测试校准。"],
                ["效率", "任务成功前提下减少冗余工具、延迟与token成本；不能奖励只拒绝所有问题。"],
                ["版本", "prompt/环境/工具/模型、reward代码与权重、rollout种子、预算、失败规则均可追溯。"],
            ], [100, CW - 100], 9.3),
            ("note", "现有evidence_precision是“无禁止证据case占比”，不是语义支持率；不能直接作为主要reward。运营采纳和CTR受曝光、时效、人群等影响，不能直接当因果收益。评测器/奖励调参用validation，test保持独立。", AMBER),
        ]),
        Page("数据划分与 manifest：防止答案泄漏", "风险层与训练切分是两个维度。此清单为单样本格式演示，approved_case_count=0。", [
            ("code", compact_lines(manifest), 8.6, 11.8),
            ("p", "file_sha256按清单声明的实际UTF-8序列化字节计算；与第3页规范JSON对象哈希的算法不同。正式交付必须校验真实文件字节，不能拿PDF文本复制后的字节直接比较。", 9.4),
            ("h2", "划分的五条规则"),
            ("p", "1. 同一事件、转载、切片、更新阶段、运营候选和近重复问题保持同split。\n2. 同一会话各轮保持同split；有交叉关联时按连通组一起划分。\n3. 同时约束观察时间；任何输入不得带未来事实、标签或投递结果。\n4. train用于训练；validation用于选Prompt/奖励/超参数；test单独冻结。\n5. Golden/Fresh/High-risk可能重叠，统计独立case/event数量；test若用于修复，登记污染并更换独立测试版本。", 9.8),
        ]),
        Page("交付目录与质量验收：业务团队照单准备", "下面是建议交付包，不是本次已生成的数据文件。先做小批试标与复核，再根据错误分布扩量。", [
            ("code", 'newsagent_data_delivery/v1/\n  manifest.json              # 版本、文件哈希、数量、用途\n  collection_ledger.jsonl    # scope/group/split/授权/审核\n  hot_news_inputs.jsonl      # 第4页业务输入\n  evaluation_labels.jsonl    # 第6页规则标签\n  candidate_targets.jsonl    # 第5页完整人工修订候选\n  retrieval_cases.jsonl      # 第8页相关性与语料引用\n  dialogue_cases.jsonl       # 第7页问题、历史及轨迹\n  annotation_guide.md        # 口径、例外、冲突与复核\n  snapshots/                # 获准内容、工具/语料快照\n  sft_train.jsonl            # 将来审核/授权后才导出\n  preference_train.jsonl     # 可选未来DPO数据', 9.3, 13.1),
            ("table", ["验收项目", "通过条件"], [
                ["完整与一致", "必填字段/枚举合法，引用存在，哈希匹配；CTR与热度可复算；缺基线/水位显式记录。"],
                ["隐私与用途", "无原始行为、密钥、个人标识、签名URL或未授权片段；用途与保留期限清晰。"],
                ["人工复核", "标注人与复核人不同；冲突/不可评排除正式冻结；完整答案评事实和语义。"],
                ["防泄漏", "split组交集为0；查近重复；无未来证据、test回流或个人偏好越作用域。"],
                ["评测报告", "失败/缺答保留固定分母；不可评数量另报；双基线同输入；分板块/风险与独立事件计数。"],
            ], [120, CW - 120], 9.2),
            ("note", "不能用合成条数或重复快照证明真实泛化；当前432运营候选来自36虚构主线，且待人工复核。质量门禁、分层数量和可接受退化幅度应在实验前审批，不能用宽松默认值当生产标准。", AMBER),
        ]),
        Page("项目接入调用链：复用哪里，补哪里", "本次交付仅为PDF与可重复生成源码；未修改业务Schema、运行配置、数据库或生产模型。", [
            ("flow", [
                "上游：热点/会话运行 + 运营显式反馈 → 采集侧完成脱敏与用途检查 → 现有AnalysisFeedbackCollector来源绑定及幂等收集 → 标签提交及独立二审。",
                "现有转换：EvaluationDatasetFreezer按cutoff读取批准快照 → prepare/upload/persist → PostgreSQL索引 + S3/MinIO不可变JSON。",
                "拟新增：完整修订答案、用途授权、事件/近重复分组与split → 数据台账校验 → SFT/DPO导出；轨迹与奖励契约另行建设。",
                "下游现有：HotNewsOfflineReplayService同输入运行候选/线上/上一实验 → EvaluationGate → ProductionBundle人工批准、激活或回滚。",
            ]),
            ("table", ["配置 / 依赖", "对应位置"], [
                ["模型与输入", "MODEL_RUNTIME_CONFIG_PATH；PromptRegistry；InferencePort；Schema/Validator/模型版本。"],
                ["回放与候选", "DATA_LOOP_REPLAY_MAX_CONCURRENCY、DATA_LOOP_MAX_CASES_PER_COHORT、HOT_NEWS_RUNTIME_MANIFEST_JSON；候选须已注册。"],
                ["存储与鉴权", "PostgreSQL、S3/MinIO、Temporal；Data Loop网关鉴权。密钥值不进入样本与PDF。"],
                ["待完成", "自动脱敏与导出验收；训练导出/API/迁移；真实数据与二审；检索/对话评测；独立TrainingPort、预算、产物注册；企业质量验收。"],
            ], [125, CW - 125], 9.1),
            ("p", "测试入口：agent/tests/test_hot_news_evaluation.py、test_dataset_freezer.py、test_offline_replay.py、test_evaluation_gate.py、test_hot_news_dialogue_eval.py。新增导出器需验证：待审/无授权/非train样本拒绝，group跨split阻断，线上渲染一致和完整目标合法。", 9.3),
        ]),
        Page("依据与本次验证：可追溯、可重复生成", "代码状态核对日期：2026-10-13。Schema/样例校验通过不等于企业模型质量或正式人工审核。", [
            ("table", ["检查", "结果"], [[k, v] for k, v in validation.items()], [270, CW - 270], 9),
            ("h2", "项目依据（均相对下列仓库根目录）"),
            ("p", str(ROOT), 8.5, GRAY),
            ("p", "agent/app/schemas/hot_news.py：输入、报告、枚举和必填Memory字段。\nagent/app/analytics/hot_score.py：默认四分量确定性计算。\nagent/app/model_runtime/core.py：StructuredInferenceService.render_input。\nagent/app/schemas/evaluation_dataset.py：标签、血缘与冻结Manifest。\nagent/app/services/data_loop/dataset_freezer.py：cutoff及二审快照冻结。\nagent/app/services/data_loop/offline_replay.py：双基线与指标真实口径。\nagent/app/conversation/query_understanding.py：问句语义与服务端裁定。\nagent/evaluation/operations_evolution.py：主线/未来证据防泄漏。", 8.5),
            ("h2", "训练格式的官方参考"),
            ("p", "https://huggingface.co/docs/trl/dataset_formats\nhttps://huggingface.co/docs/trl/sft_trainer\nhttps://huggingface.co/docs/trl/dpo_trainer\nhttps://huggingface.co/docs/trl/grpo_trainer", 8.5, BLUE),
            ("p", "生成源码：agent/evaluation/render_training_data_guide_pdf.py。CLI→build_examples→validate_examples（可选validate_business）→build_pages→Guide→ReportLab输出PDF；--output/--font/--bold-font控制输出与字体，--validate-only只校验。仅访问项目公开Schema与虚构数据，未读取.env或请求模型。", 8.5),
        ]),
    ]
    return pages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--font", type=Path, default=FONT)
    parser.add_argument("--bold-font", type=Path, default=BOLD_FONT)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--business-validation-python", type=Path,
                        help="Optional local interpreter with existing project dependencies")
    args = parser.parse_args()
    examples = build_examples()
    validation = validate_examples(examples)
    if args.business_validation_python:
        validation["business_validator_and_eval_schema"] = validate_business(
            examples, args.business_validation_python)
    if args.validate_only:
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        return
    pages = build_pages(examples, validation)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise SystemExit("Output already exists; preserve it or choose a new --output path")
    guide = Guide(args.output, args.font, args.bold_font, len(pages))
    for page in pages:
        guide.page(page)
    guide.finish()
    print(json.dumps({"output": str(args.output.resolve()), "page_count": len(pages),
                      "sha256": sha256(args.output.read_bytes()).hexdigest(),
                      "validation": validation}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
