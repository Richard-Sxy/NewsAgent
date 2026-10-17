"""One-page Chinese test guide, grounded in the executed single-layer smoke run.

Chain: CLI -> this renderer -> baseline JSON -> Diagram/ReportLab -> PDF.
Config: pinned receipt path, A3 landscape, embedded STHeiti fonts.
QA: pypdf page count and Poppler PNG inspection. No external services.
TODO: tiered benchmark, event replay, per-tier deadlines and service E2E.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent/evaluation"))
from render_real_news_warehouse_pdf import (  # noqa: E402
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, white, HexColor,
)


def render(output: Path) -> None:
    report = json.loads((ROOT / "output/tiered-recall-test-guide-2026-10-15/single-layer-ann-smoke.json").read_text())
    if report["status"] != "completed" or len(report["queries"]) != 24:
        raise ValueError("guide requires the completed 24-probe smoke run")
    latency = report["summary"][0]["strata"]["all_balanced"]["latency"]
    d = Diagram(output)
    d.c.setTitle("NewsAgent - 分层召回实际测试指导")
    d.c.setFillColor(HexColor("#FAFCFD"))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / HANDS-ON TEST GUIDE / 2026-10-15", 32, 22, 10, GREEN, True)
    d.text("先建立基线，再验证三层、内容变更与延时", 32, 43, 28, INK, True)
    d.text("从独立 CLI 开始；同一套问题与事件最后接入聊天 / 热点链路，逐步定位差异。", 32, 90, 13, MUTED)
    d.box(32, 120, 1125, 48, PALE_GREEN)
    d.text("已执行：33项相关测试通过 + 分层决策演示通过 + 真实v2索引24探针ANN基线完成。", 46, 130, 12, GREEN, True)
    d.text("后续三层动态测试仍需补实验入口；本页明确标为“待补”，不把方案写成已运行能力。", 46, 149, 10.2, MUTED)

    steps = (
        (32, "1 现有回归", "已跑通", "版本 / 去重 / 迁移顺序", GREEN, PALE_GREEN),
        (223, "2 单层基线", "已跑通", "ANN近邻重合 + p50/p95", GREEN, PALE_GREEN),
        (414, "3 三层对照", "待补入口", "全精度 → 真实压缩", BLUE, PALE_BLUE),
        (605, "4 变更回放", "待补入口", "版本 / 撤稿 / 迁移故障", BLUE, PALE_BLUE),
        (796, "5 延时矩阵", "待补入口", "首次加载 / 并发 / 注入", BLUE, PALE_BLUE),
        (987, "6 服务验收", "最后接入", "CLI → API → 聊天/热点", AMBER, PALE_AMBER),
    )
    for x, title, status, body, color, fill in steps:
        d.box(x, 191, 170, 94, fill, color)
        d.text(title, x+12, 202, 15, color, True)
        d.text(status, x+12, 231, 11, INK, True)
        d.text(body, x+12, 258, 8.8, MUTED)
    for x in (202, 393, 584, 775, 966):
        d.arrow(x, 239, x+17, 239, BLUE)

    d.box(32, 307, 545, 138, white, GREEN)
    d.text("现在就能执行", 47, 320, 16, GREEN, True)
    d.text("在仓库根目录运行：", 47, 351, 11, INK)
    d.text("bash output/tiered-recall-test-guide-2026-10-15/run_baseline.sh smoke", 47, 377, 9.5, BLUE)
    d.text("smoke：24探针 / K=10 / ef=256；full：96探针 / K=3,10,20 / ef=256,512。", 47, 401, 9.5, MUTED)
    d.text(f"本次单层实测：p50={latency['p50_ms']:.2f}ms，p95={latency['p95_ms']:.2f}ms；每次报告使用新路径。", 47, 423, 9.5, INK)

    d.box(607, 307, 550, 138, PALE_BLUE, BLUE)
    d.text("三层实验的输入与对照", 622, 320, 16, BLUE, True)
    d.para("仅选有真实日期的8,000篇 / 13,873片，复用原向量行号；日期未知先排除。\n观察点10月15日：≤1天热1,000篇，≤4天温3,000篇，其余冷4,000篇。\nA单层全精度；B三层全精度；C与B同路由、改真实压缩和加载。\n热/温/冷问题均覆盖；向量探针测近邻误差，人工相关标注测语义质量。", 622, 351, 518, 10.8, 83)

    d.text("内容变化必须逐事件验证：每步都查询、核对台账、记录时间", 32, 466, 16, INK, True)
    events = (
        (32, "新增 / 正文更正", "新ID可搜；同ID v1→v2\n重新嵌入，旧内容不可见"),
        (318, "自然降温 / 旧闻回热", "可控时钟热→温→冷\n合成聚合热度触发晋升"),
        (604, "撤稿 / 更新与迁移并发", "所有层屏蔽；仅最新版本可见\n不因迁移重复加权或漏内容"),
        (890, "迁移故障 / 恢复", "写入或验证失败保留源层\n重试幂等，重启后结果一致"),
    )
    for x, title, body in events:
        d.box(x, 503, 267, 90, PALE_GREEN, GREEN)
        d.text(title, x+13, 515, 12.5, GREEN, True)
        d.para(body, x+13, 548, 242, 10.5, 34)

    d.box(32, 616, 545, 109, PALE_BLUE, BLUE)
    d.text("测两类时间 + 一组正确性", 47, 628, 15, BLUE, True)
    d.para("查询：编码、每层搜索、加载、融合、完整返回分别计时。\n新鲜度：接收变更 → 首次满足预期状态；超时也计为失败。\n正确性：Recall@K、旧版本/撤稿残留、重复、丢失、资源占用。", 47, 660, 512, 10.8, 53)
    d.box(607, 616, 550, 109, PALE_AMBER, AMBER)
    d.text("控制变量，逐项扩大", 622, 628, 15, AMBER, True)
    d.para("固定模型/查询/seed/K/候选预算/路由；并发按1→4→8增加。\n首次进程加载与重复查询分开；不声称清空了操作系统文件缓存。\n模拟网络与积压的配置单列；冷层不保证比热层慢。", 622, 660, 518, 10.8, 53)

    d.text("先补4处：日粒度切层与适配器；FAISS落盘/懒加载修复；版本/撤稿台账；回放器与分层计时报告。", 32, 745, 11.5, INK, True)
    d.text("当前ANN报告只测已存文档向量的近邻重合与单线程热缓存耗时，不含BGE编码、HTTP、人工语义质量或三层变更新鲜度。", 32, 772, 10.1, MUTED)
    d.text("先看正确性，再比较延时；更多请求再观察尾部，24个探针不能证明稳定的p99或企业容量。", 32, 795, 10.5, AMBER)
    d.text("入口：run_baseline.sh → pytest / vector_tier_policy → evaluate_bge_ann → 独立JSON；生成链：回执 → ReportLab → 单页PDF。", 32, 821, 9, MUTED)
    d.c.showPage()
    d.c.save()


if __name__ == "__main__":
    output = ROOT / "output/pdf/NewsAgent_分层召回实际测试指导_2026-10-15.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    render(output)
    print(output)
