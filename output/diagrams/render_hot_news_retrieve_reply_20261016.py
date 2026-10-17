"""One-page explanation of the current implemented retrieval-to-reply chain.

Entry: run this script. Input: inspected HotNewsEnrichment, behavior runtime,
analysis builder/runner/validator code. render() turns that chain into diagram
cards using ReportLab/Diagram and local Chinese fonts, then writes OUTPUT.
Config: ROOT/OUTPUT constants; business settings are documented on the page.
Checks: pdfinfo one-page confirmation + pdftoppm visual review. No runtime edit.
TODO: natural-language behavior-filter routing and production enablement remain
outside this explanatory artifact; no graph database query is implemented.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "output/pdf/newsagent-hot-news-retrieve-and-reply-2026-10-16.pdf"
sys.path.insert(0, str(ROOT / "agent/evaluation"))
from render_real_news_warehouse_pdf import (
    Diagram, W, H, INK, MUTED, BLUE, GREEN, AMBER, GRAY,
    PALE_BLUE, PALE_GREEN, PALE_AMBER,
)
from reportlab.lib.colors import HexColor, white


def render():
    d = Diagram(OUTPUT)
    d.c.setTitle("热点Agent如何检索行为标签并生成回复")
    d.c.setFillColor(HexColor("#FAFCFD")); d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / RETRIEVAL TO REPLY / 2026.10.16", 32, 24, 10, GREEN, True)
    d.text("先取数与找证据，再让模型解释", 32, 49, 30, INK, True)
    d.text("例：分析 10 月 14 日 19-20 时的热点，说明表现及跟进建议。", 32, 99, 14, MUTED)

    d.box(32, 145, 538, 104, PALE_BLUE, BLUE)
    d.text("新闻与聚合指标", 49, 159, 18, BLUE, True)
    d.para("数仓：曝光、点击、有效消费、互动与基线\n内容库 / 知识库：标题、正文、相关报道", 49, 195, 500, 12, 43)
    d.box(608, 145, 549, 104, PALE_AMBER, AMBER)
    d.text("本地标签与合成画像包", 625, 159, 18, AMBER, True)
    d.para("读取不晚于窗口结束的画像快照，聚合主题兴趣\n用冻结规则识别当前新闻与候选新闻的共享主题", 625, 195, 510, 12, 43)
    d.route([(301, 249), (301, 270), (134, 270), (134, 300)], BLUE)
    d.route([(882, 249), (882, 270), (595, 270), (595, 300)], AMBER)

    steps = [
        ("1 取数与排名", "确定租户和时间\nSQL 取聚合指标\nPython 计算热度\n生成热点候选", "SQL / Python", BLUE, PALE_BLUE),
        ("2 找相关新闻", "按标题、摘要召回\n补全正文与来源\n检查内容资格\n执行原相关性评分", "KnowledgeSearch", BLUE, PALE_BLUE),
        ("3 兴趣辅助重排", "读取聚合兴趣权重\n匹配共享主题\n最多加 0.08\n保留主体冲突约束", "BehaviorContext", AMBER, PALE_AMBER),
        ("4 模型组织解释", "指标与热度\n新闻正文片段\n选中的相关证据\n排序理由 → Prompt", "AnalysisInput → Model", GREEN, PALE_GREEN),
        ("5 校验并返回", "检查结构与新闻ID\n校验证据引用\n保存分析及排序轨迹\n工作台展示结果", "Validator → Report", GREEN, PALE_GREEN),
    ]
    for i, (title, body, code, color, fill) in enumerate(steps):
        x = 32+i*229
        d.box(x, 300, 209, 205, fill, color)
        d.text(title, x+13, 316, 16, color, True)
        d.para(body, x+13, 356, 182, 12, 100)
        d.text(code, x+13, 477, 9.2, MUTED)
        if i < 4:
            d.arrow(x+209, 403, x+222, 403, GREEN)

    d.box(32, 533, 694, 173, white, GREEN)
    d.text("最后的回复包含什么？", 49, 548, 20, GREEN, True)
    d.para("当前新闻的表现与趋势解释、引用哪些相关报道、运营跟进建议、证据不足的限制。\n\n数字与热点分数来自 SQL / Python；兴趣标签主要影响选入哪些相关新闻证据。\n模型接收证据摘要与排序理由，不接收原始个人画像或合成用户ID。", 49, 590, 656, 13, 103)
    d.box(751, 533, 406, 173, white, AMBER)
    d.text("单独查行为标签", 768, 548, 19, AMBER, True)
    d.para("POST /api/v1/behavior-tags/search\n按主题、行为标签、时间、来源筛选\n返回新闻、窗口指标和标签依据\n\n当前与热点自动重排分别提供；尚未自动把任意自然语言映射成这套行为过滤。", 768, 590, 371, 11.5, 102)

    d.box(32, 731, 1125, 62, PALE_AMBER)
    d.para("当前状态：检索代码已接入，默认关闭，既有在线实例未重启；启用需 API 与 Worker 读取同一数据包。\n配置：BEHAVIOR_TAGS_ENABLED + BEHAVIOR_TAGS_PACKAGE_PATH。前页知识图谱是关系示意，运行时使用 JSONL / SQLite。", 47, 742, 1091, 11, 40, AMBER)
    d.text("代码依据：hot_news_enrichment.py  /  behavior_tags/runtime.py  /  services/hot_news_analysis.py  /  model_runtime/hot_news.py", 32, 810, 9.2, MUTED)
    d.c.showPage(); d.c.save()
    print(OUTPUT)


if __name__ == "__main__":
    render()
