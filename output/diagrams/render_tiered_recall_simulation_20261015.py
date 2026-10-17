"""Render the inspected local tiered-recall simulation design as one Chinese page.

Input: the v2 verification receipt and structured corpus publication timestamps.
Configuration: explicit simulation clock and 1/4-day demo thresholds only.
Dependencies: existing Diagram helper, ReportLab, and embedded STHeiti fonts.
Output: one PDF; QA uses pypdf page counting plus Poppler visual rendering.
TODO: the scenario replayer, tier adapter, and latency benchmark are proposed.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent/evaluation"))
from render_real_news_warehouse_pdf import (  # noqa: E402
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, white, HexColor,
)


def evidence() -> tuple[dict, Counter]:
    receipt = json.loads((ROOT / "output/news-bulk-import-2026-10-15/bge-verification.json").read_text())
    if receipt["status"] != "verified":
        raise ValueError("the figure requires a successful v2 verification receipt")
    corpus = ROOT / "agent/data/news-bge-incremental-20261014-v2/data/experiments/offline_recall_fg/chunks.jsonl"
    articles = {}
    for line in corpus.open():
        row = json.loads(line)
        articles.setdefault(row["news_id"], row.get("publish_time"))
    if len(articles) != receipt["total_articles"]:
        raise ValueError("article inventory differs from v2 receipt")
    counts = Counter()
    now = date(2026, 10, 15)
    for published in articles.values():
        if not published:
            counts["unknown"] += 1
            continue
        age = (now - date.fromisoformat(published[:10])).days
        counts["hot" if age <= 1 else "warm" if age <= 4 else "cold"] += 1
    if dict(counts) != {"unknown": 12187, "hot": 1000, "warm": 3000, "cold": 4000}:
        raise ValueError("the explicitly dated simulation population has changed")
    return receipt, counts


def render(output: Path) -> None:
    receipt, counts = evidence()
    d = Diagram(output)
    d.c.setTitle("NewsAgent - 本地冷温热分层召回模拟")
    d.c.setSubject("代码核查结论与拟议实验；未部署三层索引或声称生产性能")
    d.c.setFillColor(HexColor("#FAFCFD"))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / TIERED RECALL / 2026-10-15", 32, 22, 10, GREEN, True)
    d.text("本地可以模拟：资源差异 × 查询延时 × 内容演变", 32, 43, 27, INK, True)
    d.text("已有分层实验和原生检索接口；当前服务仍是单层只读 BGE 快照，需要补接动态实验。", 32, 88, 13, MUTED)

    d.box(32, 119, 1125, 57, PALE_AMBER)
    d.text("先把数据与实验口径分清", 46, 129, 12, AMBER, True)
    d.text(f"v2：{receipt['total_articles']:,} 篇 / {receipt['total_chunks']:,} 片 / 1024维；仅8,000篇有结构化日期，另12,187篇日期未知。", 46, 151, 11, INK)

    columns = (
        (32, GREEN, PALE_GREEN, "热层 HOT", f"{counts['hot']:,} 篇 · 年龄 ≤ 1天", "常驻内存 / 全精度 HNSW", "新稿、追踪更新；优先快速可见"),
        (414, BLUE, PALE_BLUE, "温层 WARM", f"{counts['warm']:,} 篇 · 1 < 年龄 ≤ 4天", "量化索引 / 按需加载", "降温、增量修订；记录加载代价"),
        (796, AMBER, PALE_AMBER, "冷层 COLD", f"{counts['cold']:,} 篇 · 年龄 > 4天", "紧凑索引 / 磁盘归档演练", "历史回溯、旧闻回热；更正仍要及时"),
    )
    for x, color, fill, title, count, storage, updates in columns:
        d.box(x, 194, 361, 121, fill, color)
        d.text(title, x+16, 206, 18, color, True)
        d.text(count, x+16, 236, 13, INK, True)
        d.text(storage, x+16, 263, 12, INK)
        d.text(updates, x+16, 289, 10.5, MUTED)
    d.text("以上为演练参数：原始发布时间保留；以10月15日为观察点。原30天/1年规则下，这8,000篇全部在热层。", 32, 326, 10.5, MUTED)

    d.text("查询链：用同一问题、同一向量与同一候选预算比较", 32, 354, 16, INK, True)
    query_boxes = (
        (32, 173, "问题 → BGE向量", "单独记录编码时间"),
        (236, 218, "路由 / 分层预算", "全量并发 vs 按需补召回"),
        (485, 218, "热 / 温 / 冷检索", "分层耗时、首次加载、超时"),
        (734, 197, "有效版本复核", "撤稿屏蔽 + 去重 + RRF"),
        (962, 195, "证据 / 答复", "层来源、版本、完整性"),
    )
    for x, width, title, subtitle in query_boxes:
        d.box(x, 389, width, 68, white, BLUE)
        d.text(title, x+12, 400, 13, BLUE, True)
        d.text(subtitle, x+12, 429, 10, MUTED)
    for x1, x2 in ((205, 229), (454, 478), (703, 727), (931, 955)):
        d.arrow(x1, 423, x2, 423, BLUE)
    d.text("全层并发完整返回约等于：编码 + 最慢层 + 融合；冷层变慢仍会拖住整体。不能只用候选数量决定停止。", 32, 468, 10.7, AMBER)

    d.text("变更链：用可控时钟回放，几分钟观察跨天生命周期", 32, 496, 16, INK, True)
    d.box(32, 531, 1125, 85, PALE_GREEN, GREEN)
    for x, title, text in (
        (48, "新稿 / 更新", "热层v1 → 热层v2"),
        (273, "自然降温", "热 → 温 → 冷"),
        (498, "旧闻回热", "冷 → 温 → 热"),
        (723, "历史更正", "新版本先可见，再归并"),
        (948, "撤稿 / 迁移失败", "全层屏蔽 / 源层保留"),
    ):
        d.text(title, x, 543, 13, GREEN, True)
        d.text(text, x, 577, 11, INK)
    d.text("台账保存 news_id + 内容版本 + Embedding版本；迁移先写、验证、再删。正文更正不能仅搬移旧向量。", 32, 626, 10.5, MUTED)

    d.box(32, 658, 545, 104, PALE_BLUE, BLUE)
    d.text("两类延时必须分开量", 47, 669, 15, BLUE, True)
    d.para("查询延时：各层 / 完整返回 p50、p95、p99，首次加载与重复查询分开。\n变更可见延时：接收新稿或更正 → 能检索；接收撤稿 → 全层不可见。\n同时看Recall@K、旧版本命中、重复率、积压、真实索引体积与内存。", 47, 698, 514, 10.6, 59)
    d.box(607, 658, 550, 104, PALE_AMBER, AMBER)
    d.text("本地实测 + 受控故障注入，各自标记", 622, 669, 15, AMBER, True)
    d.para("实测：本机搜索、量化、读盘、更新与资源竞争。\n注入：网络等待、冷层慢请求、写入积压、迁移中断。\n冷索引常驻后也可能很快；注入结果不能当企业集群性能。", 622, 698, 518, 10.6, 59)

    d.text("实施缺口：日粒度分层适配 + 版本/撤稿台账 + 场景回放 + 分层计时；聊天串行与热点并发两条路径需对齐。", 32, 778, 10.6, INK, True)
    d.text("核查：29项相关离线测试通过；FAISS落盘需修复，hnswlib量化账面体积不等于真实内存。变更事件均为合成演练。", 32, 800, 10, MUTED)
    d.text("证据：v2验收回执 / BGE快照适配器 / tiered_vector / news实验目录   |   设计建议，尚未执行完整动态压测   |   1 / 1", 32, 822, 9, MUTED)
    d.c.showPage()
    d.c.save()


if __name__ == "__main__":
    output = ROOT / "output/pdf/NewsAgent_本地冷温热分层召回模拟_2026-10-15.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    render(output)
    print(output)
