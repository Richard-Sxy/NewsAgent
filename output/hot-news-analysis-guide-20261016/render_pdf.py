"""Render the audited, unactivated prompt package as a single-page Chinese diagram."""
from pathlib import Path
from hashlib import sha256
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent/evaluation"))
from render_real_news_warehouse_pdf import (
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, white, HexColor,
)
from pypdf import PdfReader


def main():
    package = Path(__file__).resolve().parent
    manifest = json.loads((package / "manifest.json").read_text())
    verification = json.loads((package / "verification.json").read_text())
    assert verification["status"] == "passed" and verification["check_count"] == 13
    assert not verification["model_called"] and not verification["runtime_activated"]
    assert sha256((package / "hot-news-analysis.system-prompt.txt").read_bytes()).hexdigest() == manifest["candidate_prompt_sha256"]
    for path, digest in manifest["source_code"].items():
        assert sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
    output = ROOT / "output/pdf/NewsAgent_热点分析判断依据与提示词_2026-10-16.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    d = Diagram(output)
    c = d.c
    c.setTitle("NewsAgent热点分析：判断依据、现有链路与候选提示词")
    c.setSubject("当前代码核对；候选提示词；会话baseline/trend与单篇报告输入边界")
    c.setFillColor(HexColor("#FAFCFD"))
    c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / HOT NEWS REASONING & PROMPT / 2026-10-16", 32, 23, 10, GREEN, True)
    d.text("热点分析：事实计算 → 评分解释 → 有据建议", 32, 48, 28, INK, True)
    d.text("Python决定指标和得分；提示词要求模型解释当前表现、新闻背景、局限与建议。", 32, 93, 14, MUTED)
    d.box(32, 123, 1125, 31, PALE_AMBER)
    d.text("当前本地模型是规则替身；增强提示词为候选资产，未激活。13项离线接线检查通过，尚未做真实模型质量评测。", 45, 131, 11.2, AMBER)

    steps = (
        (32, "1 取数与范围", "租户 + news_id + 固定窗口\n只读SQL / 聚合指标\n窗口与身份核验"),
        (286, "2 Python权威热度", "30% 点击/曝光\n35% 有效消费/点击\n20% 互动/有效消费\n15% 压缩增长分；候选池内排名"),
        (540, "3 正文与关联证据", "news_id精确查正文\n检索与确定性重排\n最多5条关联证据；可选Memory"),
    )
    for x, title, body in steps:
        d.box(x, 185, 232, 126, PALE_BLUE, BLUE)
        d.text(title, x + 14, 199, 16, BLUE, True)
        d.para(body, x + 14, 235, 205, 11.1, 65)
        if x < 540:
            d.arrow(x + 232, 249, x + 248, 249, BLUE)
    d.arrow(656, 311, 656, 335, BLUE)

    d.box(32, 341, 740, 104, white, GREEN)
    d.text("4 HotNewsAnalysisInput：模型实际收到的判断依据", 48, 354, 16, GREEN, True)
    d.para("当前6项指标 + 热度总分/四分量 + 窗口 + 标题/摘要/正文片段 + 关联证据 + 可选记忆\n没有原始基线详情/n、短期序列、历史高点或排名字段，不能补造这些依据。", 48, 389, 704, 11.6, 44)
    d.arrow(402, 445, 402, 469, GREEN)
    d.box(32, 475, 740, 87, PALE_GREEN, GREEN)
    d.text("5 模型入口：版本化Prompt + 受限输入", 48, 488, 17, GREEN, True)
    d.para("NativeHotNewsAnalysisRunner → PromptRegistry → StructuredInferenceService\nsystem：候选提示词 + 输出JSON Schema；user：输入JSON，新闻材料保持为数据。", 48, 522, 704, 11.2, 38)
    d.arrow(402, 562, 402, 585, GREEN)
    d.box(32, 591, 740, 67, white, GREEN)
    d.text("6 结构与引用门禁 → 同运行报告 → API / 会话 / 页面", 48, 602, 15.5, GREEN, True)
    d.text("检查Schema、新闻身份、指标名、证据/Memory ID；自由文本数字、因果与建议仍需评测。", 48, 634, 10.7, MUTED)

    d.box(798, 185, 359, 473, PALE_GREEN, GREEN)
    d.text("增强候选提示词 · 摘要", 815, 199, 17, GREEN, True)
    d.text("完整系统提示词见附带TXT，可复制。", 815, 230, 10.7, MUTED)
    d.para("你是NewsAgent热点解释模块，用中文解释本次快照并输出结构化建议。\n\n1. 只解释输入数字和已加权分量。\n2. growth是得分贡献，不是增长率。\n3. growth=0不能推出持平或无历史。\n4. 缺少短期序列与峰值，不判断爆发、回落或创新高。\n5. 区分量化事实、报道背景和原因假设。\n6. 证据与Memory ID仅来自输入白名单。\n7. 建议列明行动、依据和优先级，供人工审核。\n8. 按HotNewsAnalysisReport Schema只输出JSON，缺口写limitations。", 815, 260, 323, 12.1, 313)
    d.text("输入原文与Schema分开；不把新闻当指令。", 815, 615, 10.5, MUTED)
    d.text("候选版本：20261016 / 离线render校验13项", 815, 637, 9.8, MUTED)

    d.box(32, 686, 520, 100, PALE_BLUE, BLUE)
    d.text("会话分析支线：已经有 baseline / trend", 48, 698, 16, BLUE, True)
    d.para("已完成运行 → analyze_hot_news_data → Python比较 → 会话解释\nbaseline对照已保存参考字段；trend比较两次运行共同上榜新闻。\n双窗口不一定相邻，不代表全站趋势；结果尚未自动注入单篇报告。", 48, 732, 486, 10.4, 50)
    d.box(576, 686, 581, 100, PALE_AMBER, AMBER, dashed=True)
    d.text("待补入报告的依据：版本化接线与真实模型评测", 592, 698, 16, AMBER, True)
    d.para("原始基线 / 样本数 / 版本；可比短期变化；指定回看范围的历史高点。\n先扩展输入Schema、引用键和确定性计算，再显式注入工具结果。\nPrompt已交付，不能仅靠改提示词补齐不存在的数据。", 592, 732, 547, 10.6, 50)
    d.text("调用链与配置：入口 → SQL/Python → InputBuilder → Runner/PromptRegistry → InferencePort → Pydantic/Validator → analysis_runs；MODEL_RUNTIME_CONFIG_PATH + Bundle选版本。", 32, 803, 8.8, MUTED)
    d.text("来源：现有模型输入/运行时/会话分析代码；OpenAI Docs提示结构指南。制图：ReportLab；pypdf/Poppler校验单页与中文。", 32, 821, 8.7, MUTED)
    c.linkURL("https://developers.openai.com/api/docs/guides/prompt-engineering", (470, 5, 690, 22), relative=0)
    c.showPage()
    c.save()
    reader = PdfReader(output)
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert all(token in text for token in ("判断依据", "growth", "13项", "baseline / trend", "未激活", "PromptRegistry"))
    print(json.dumps({"output": str(output), "pages": 1, "bytes": output.stat().st_size, "chinese_text": "passed"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
