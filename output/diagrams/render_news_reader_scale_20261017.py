"""Proposed reader-conditioned audience pipeline, not an implementation claim.

Entry/config: run this script; ROOT and OUTPUT select the local PDF destination.
Inputs: current runtime/audience code audit + linked primary documentation.
Transform/core: render() lays out enterprise aggregation and bounded online reads.
Dependencies: ReportLab, existing Diagram helper, local Chinese fonts; no clients.
Downstream: one-page PDF. Checks: assertions, pdfinfo and pdftoppm visual review.
TODO: NewsReaderProfilePort, upstream aggregate contract and scale benchmarks.
No runtime/config/data package changes, no enterprise personal records accessed.
"""

from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "output/pdf/newsagent-news-reader-million-scale-design-2026-10-17.pdf"
sys.path.insert(0, str(ROOT / "agent/evaluation"))
from render_real_news_warehouse_pdf import (
    Diagram, W, H, INK, MUTED, BLUE, GREEN, AMBER, GRAY,
    PALE_BLUE, PALE_GREEN, PALE_AMBER,
)
from reportlab.lib.colors import HexColor, white


def render():
    assert Decimal(4_800_000)/8_000_000 == Decimal("0.6")
    assert Decimal("0.6")/Decimal("0.2") == Decimal(3)
    assert Decimal(2_400_000)/8_000_000/Decimal("0.05") == Decimal(6)
    d = Diagram(OUTPUT)
    d.c.setTitle("热点新闻读者画像与推送分析 - 百万千万级目标架构")
    d.c.setFillColor(HexColor("#FAFCFD")); d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / READER AUDIENCE / SCALE DESIGN / 2026.10.17", 32, 21, 10, GREEN, True)
    d.text("千万访客参与统计，Agent 在线读取聚合快照", 32, 45, 28, INK, True)
    d.text("业务目标：热点新闻 → 访问它的人主要有什么兴趣 → 目标人群预估 → 推送分析与人工决策", 32, 89, 13, MUTED)
    d.box(32, 119, 1125, 38, PALE_AMBER)
    d.text("当前已有：本地合成标签与总体兴趣重排。下图是待补建的按新闻访客聚合方案；尚无百万 / 千万级延时验收。", 45, 131, 11.4, AMBER)

    # Enterprise-owned high-volume preparation path.
    d.box(32, 178, 1125, 153, PALE_BLUE)
    d.text("企业数据侧：持续 / 批量计算，个人明细保留在企业系统", 47, 188, 15, BLUE, True)
    preparation = [
        ("行为流 + 已有画像", "版本化访问口径\n身份规则与稳定兴趣"),
        ("去重与时点关联", "新闻 × 用户 × 窗口\n排除本篇对兴趣的贡献"),
        ("按新闻聚合标签", "标签覆盖人数 / 基准率\nUV 状态、覆盖率、修订"),
        ("快照与服务索引", "按新闻 / 窗口读取\n常用窗口预热与缓存"),
    ]
    for i, (title, body) in enumerate(preparation):
        x=47+i*280
        d.box(x, 224, 250, 86, white, BLUE)
        d.text(title, x+12, 235, 15, BLUE, True)
        d.para(body, x+12, 265, 226, 11, 37)
        if i < 3:
            d.arrow(x+250, 268, x+272, 268, BLUE)

    # Online path consumes bounded summaries, not per-reader profiles.
    d.text("NewsAgent 在线路径（目标设计）", 32, 349, 16, GREEN, True)
    online = [
        ("热点 Top N", "SQL / Python 排名\n确定新闻与窗口"),
        ("批量读访客画像", "建议新增聚合 Port\n一次查询多篇新闻"),
        ("校验聚合证据", "Top 标签 / lift\n版本、覆盖率、水位"),
        ("目标受众预览", "兴趣 + 通知资格\n频控 / 排除已读"),
        ("生成推送分析", "LLM 解释证据\n人工确认后续动作"),
    ]
    for i, (title, body) in enumerate(online):
        x=32+i*229
        d.box(x, 384, 209, 103, PALE_GREEN, GREEN)
        d.text(title, x+12, 397, 15, GREEN, True)
        d.para(body, x+12, 431, 185, 11.5, 39)
        if i < 4:
            d.arrow(x+209, 435, x+222, 435, GREEN)
    d.route([(1012, 310), (1012, 337), (367, 337), (367, 384)], BLUE)
    d.text("仅返回聚合结果；不回传用户名单", 511, 344, 10, BLUE)

    cards = [(32, 357, BLUE), (406, 357, GREEN), (780, 377, AMBER)]
    for x, w, color in cards:
        d.box(x, 512, w, 199, white, color)
    d.text("口径：三个对象分别回答", 47, 526, 17, BLUE, True)
    d.para("内容标签：这篇讲什么？\n访客画像：看过它的人原本喜欢什么？\n推送受众：接下来适合推给谁？\n\n同一读者同一标签只计一次；多标签可重叠。\n画像缺失单列；窗口 UV 不能直接相加。\n稳定 / 短期兴趣分别统计，绑定画像时点。", 47, 564, 326, 11.5, 133)

    d.text("千万访客示例（假设数据）", 421, 526, 17, GREEN, True)
    d.para("去重访客 1,000 万，有画像 800 万，覆盖率 80%。\n科技：480 万 / 800 万 = 60%，基准 20%\n→ lift = 3；AI：30% / 基准 5% → lift = 6\n\n据此解释兴趣方向，再查询可推人群。\n不能把已读人数直接当可投递人数，\n也不能由 lift 推导因果或预计点击率。", 421, 564, 326, 11.5, 133)

    d.text("延时预算（建议，未实测）", 795, 526, 17, AMBER, True)
    d.para("缓存命中：聚合接口 P95 ≤ 100 ms\n预聚合回源：批量接口 P95 ≤ 500 ms\n结构化数据首屏：P95 ≤ 1 s（不含模型）\n\n更新周期可先按 30-60 s 设计，展示水位。\n完整模型分析异步更新，单独统计耗时。\n常用窗口快查，复杂窗口异步计算。", 795, 564, 346, 11.5, 133)

    d.box(32, 731, 1125, 61, PALE_AMBER)
    d.para("关键工程点：热点按稳定用户分片；批量接口；版本化缓存与请求合并；迟到按 revision 修正；超时显示数据更新中。\n去重估计可用可合并草图；精确圈人用集合 / 位图。先合并完整标签统计再取 Top K，缺失缓存不能当零人。", 46, 742, 1095, 11, 39, AMBER)
    refs = [
        ("ClickHouse：增量聚合", "https://clickhouse.com/docs/concepts/features/materialized-views/incremental-materialized-view"),
        ("Flink：迟到窗口修正", "https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/operators/windows/#allowed-lateness"),
        ("DataSketches：集合估计", "https://datasketches.apache.org/docs/Theta/ThetaSketchSetOpsAccuracy.html"),
        ("Redis：位图运算", "https://redis.io/docs/latest/commands/bitop/"),
    ]
    d.text("参考能力（不代表腾讯内部技术选型）：", 32, 809, 9, MUTED)
    x=292
    for title, url in refs:
        d.text(title, x, 809, 9, BLUE)
        d.c.linkURL(url, (x, H-823, x+193, H-806), relative=0)
        x+=213
    d.c.showPage(); d.c.save()
    print(OUTPUT)


if __name__ == "__main__":
    render()
