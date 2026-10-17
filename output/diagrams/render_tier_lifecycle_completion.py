"""One-page implementation handoff, backed by local verification evidence."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent/evaluation"))
from render_real_news_warehouse_pdf import (
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, GRAY, HexColor, white,
)


def main():
    evidence = json.loads((ROOT / "output/tier-lifecycle-implementation-20261015/verification.json").read_text())
    replay = json.loads((ROOT / "output/tier-lifecycle-replay-final-20261015/report.json").read_text())
    assert evidence["status"] == replay["status"] == "passed"
    assert all(item["passed"] for item in replay["checks"])
    output = ROOT / "output/pdf/NewsAgent_三层配置与状态迁移实现_2026-10-15.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    d = Diagram(output)
    d.c.setTitle("NewsAgent 三层配置与状态迁移实现")
    d.c.setFillColor(HexColor("#F7FAFC"))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / TIER LIFECYCLE / IMPLEMENTATION", 32, 23, 10, GREEN, True)
    d.text("三层数据 + 状态 + 迁移：已落地的首版链路", 32, 44, 28, INK, True)
    d.text("2026-10-15  ·  Python 原生实现  ·  显式启用  ·  企业参数由你填写", 32, 86, 13, MUTED)
    d.box(32, 113, 1126, 33, PALE_GREEN)
    d.text("已验证本地生命周期、真实 PostgreSQL 并发/迁移和 Temporal 派发；企业 Milvus 性能仍需实测。", 46, 121, 13, GREEN)

    d.text("01  三份配置，各管一件事", 32, 164, 17, INK, True)
    cards = [
        (32, "模型接口", "MODEL_RUNTIME_CONFIG_PATH", "模型路由 / 实际维度 / Embedding 批量\n企业接口与鉴权环境变量名", PALE_BLUE, BLUE),
        (414, "向量三集合", "KNOWLEDGE_SEARCH_CONFIG_PATH", "news_hot / news_warm / news_cold\n不同集合；维度、度量与模型一致", PALE_BLUE, BLUE),
        (796, "生命周期策略", "KNOWLEDGE_LIFECYCLE_CONFIG_PATH", "Th / Tc / 回热保持期 / 扫描周期\n切片上限 / 重试 / 查询候选与总超时", PALE_GREEN, GREEN),
    ]
    for x, title, env, detail, fill, color in cards:
        d.box(x, 198, 362, 111, fill, color)
        d.text(title, x+15, 210, 16, color, True)
        d.text(env, x+15, 237, 9, MUTED)
        d.para(detail, x+15, 257, 332, 11.5, 40)

    d.text("02  分层按业务时间；状态由事件驱动", 32, 328, 17, INK, True)
    tiers = [(32, "热 HOT", "0 ≤ age < Th", PALE_AMBER, AMBER),
             (414, "温 WARM", "Th ≤ age < Tc", PALE_GREEN, GREEN),
             (796, "冷 COLD", "age ≥ Tc", PALE_BLUE, BLUE)]
    for x, title, rule, fill, color in tiers:
        d.box(x, 365, 362, 65, fill, color)
        d.text(title, x+17, 377, 17, color, True)
        d.text(rule, x+171, 380, 14, INK)
    d.arrow(394, 399, 409, 399, GRAY)
    d.arrow(776, 399, 791, 399, GRAY)
    d.text("定时扫描降层；显式回热 → HOT；保持期到期 → 按年龄重新分层。", 46, 442, 12, MUTED)
    d.box(32, 470, 551, 72, white, GRAY)
    d.text("新闻状态", 48, 480, 13, GREEN, True)
    d.text("pending → active（API: ready）→ retracted", 48, 504, 13, INK)
    d.box(607, 470, 551, 72, white, GRAY)
    d.text("任务阶段与失败", 623, 480, 13, BLUE, True)
    d.text("pending → embedded → active → complete", 623, 504, 12, INK)
    d.text("暂时失败 retry；耗尽 failed；显式重试创建新代次", 623, 526, 9.5, MUTED)

    d.text("03  先写目标、验证、切换，再清理源层", 32, 561, 17, INK, True)
    steps = [("内容 API", "正文 + 单调修订号"), ("PG 台账", "冻结配置 + 任务"),
             ("Temporal", "派发 / 重试 / 扫描"), ("目标向量层", "切片 → 模型 → 验证"),
             ("PG 条件激活", "校验 generation"), ("清理旧投影", "独立 ID / 可重扫")]
    for i, (title, note) in enumerate(steps):
        x = 32+i*191
        d.box(x, 596, 171, 67, white, GREEN)
        d.text(title, x+85.5, 606, 13, GREEN, True, True)
        d.text(note, x+85.5, 635, 9.4, MUTED, center=True)
        if i < 5:
            d.arrow(x+171, 629, x+186, 629, GREEN)
    d.para("更正：先屏蔽旧版，新版激活再返回。  撤稿：状态先生效，后台删向量。\n查询：一次 Embedding → 三层并发 → 状态过滤 / 补搜 → 最终复核 → 去重排序；热点正文同样受控。", 46, 676, 1094, 11.5, 42)

    d.box(32, 730, 704, 65, PALE_GREEN)
    d.text(f"验收：后端 {evidence['backend_suite']['passed']} 通过 / {evidence['backend_suite']['skipped']} 跳过；真实基础设施 7 项通过", 46, 741, 13, GREEN, True)
    d.text(f"本地回放 {len(replay['checks'])} 项通过：三层分布、更正、回热、撤稿、乱序、重启恢复", 46, 768, 11, INK)
    d.box(756, 730, 402, 65, PALE_AMBER)
    d.text("下一步：填配置 → 数据库迁移 → 启动 Worker", 770, 741, 11.5, AMBER, True)
    d.text("先看一条新闻到 ready，再开始真实延时压测。", 770, 768, 11, INK)
    d.text("范围：新闻正文；QA 子文档暂不支持。历史清单归档、企业性能及长稳验收待做。  |  单页图解 1/1", 32, 811, 9.5, MUTED)
    d.c.showPage()
    d.c.save()
    print(output)


if __name__ == "__main__":
    main()
