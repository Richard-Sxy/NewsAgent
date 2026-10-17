"""Read local verification evidence and render one-page Chinese readiness map.

Chain: verification.json -> render() -> Diagram/ReportLab -> one-page PDF.
Config: fixed input/output paths, A3 landscape, embedded STHeiti Chinese fonts.
QA entry: execute this CLI, check with pypdf, render with Poppler for inspection.
TODO: business gaps listed in input; no runtime code or configuration is changed.
"""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent/evaluation"))
from render_real_news_warehouse_pdf import (  # noqa: E402
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, white, HexColor,
)


def render(evidence: dict, output: Path) -> None:
    total = sum(run["passed"] for run in evidence["test_runs"])
    assert all(run["exit_code"] == 0 for run in evidence["test_runs"])
    assert evidence["health_check"]["http_status"] == 200
    d = Diagram(output)
    d.c.setTitle("NewsAgent - 进入召回延时测试前的状态核查")
    d.c.setFillColor(HexColor("#FAFCFD"))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / READINESS CHECK / 2026-10-15", 34, 24, 11, GREEN, True)
    d.text("可以开始测召回延时；动态三层仍需补齐", 34, 53, 29, INK, True)
    d.text("本轮结论基于相关回归与健康检查。上一轮是实现指导，尚未把设计中的状态与迁移链路接入。", 34, 101, 13, MUTED)

    d.box(34, 145, 547, 122, PALE_GREEN, GREEN)
    d.text(f"{total} 项相关测试通过", 52, 160, 24, GREEN, True)
    d.para("检索 / 索引 / 模型配置：66 passed\n热点运行 / 编排 / SQL绑定 / 对话查询：46 passed", 52, 207, 510, 13, 42)
    d.box(609, 145, 547, 122, PALE_GREEN, GREEN)
    d.text("本地 /ready：HTTP 200", 627, 160, 24, GREEN, True)
    d.para("PostgreSQL：ok     Redis：ok     Temporal：ok\n健康检查不替代一次新的页面 → Worker → 报告业务验收。", 627, 207, 510, 12, 42)

    d.text("当前可作为延时基线的业务主干", 34, 291, 17, INK, True)
    nodes = [
        (34, "查询入口", "可信身份 / SQL取数"),
        (264, "热点编排", "指标 / 排名 / 正文"),
        (494, "关联召回", "Embedding / 检索"),
        (724, "模型与校验", "结构化输出 / 证据"),
        (954, "结果展示", "保存快照 / API"),
    ]
    for x, title, body in nodes:
        d.box(x, 330, 202, 79, PALE_BLUE, BLUE)
        d.text(title, x+13, 343, 16, BLUE, True)
        d.text(body, x+13, 376, 11.5, MUTED)
    for x in (236, 466, 696, 926):
        d.arrow(x, 370, x+21, 370, BLUE)
    d.text("本地模拟验证已有依据；本轮未发起新业务运行，也未调用企业模型或企业向量服务。", 34, 422, 11, MUTED)

    d.box(34, 458, 547, 174, PALE_BLUE, BLUE)
    d.text("现在做：固定数据的召回延时基线", 52, 474, 18, BLUE, True)
    d.para("1  固定数据包、模型、查询集与同一个检索入口\n2  分别记录Embedding、向量搜索、融合、端到端耗时\n3  同时记录p50/p95、错误率、结果数与召回质量\n4  增加并发，观察排队、资源与尾延时", 52, 514, 509, 12.5, 90)
    d.box(609, 458, 547, 174, PALE_AMBER, AMBER)
    d.text("动态三层验收前，还需要实现", 627, 474, 18, AMBER, True)
    d.para("持久索引任务与内容事件联动\n最新版本 / 撤稿 / 有效层校验\n复制 → 验证 → 条件激活 → 清理的可靠迁移\n统一查询策略，并补重复、乱序、崩溃恢复回放", 627, 514, 509, 12.5, 90)

    d.text("下一阶段按这个顺序推进", 34, 658, 17, INK, True)
    stages = [
        (34, "① 单层固定快照", "先确定现有耗时与召回基线"),
        (417, "② 三层静态查询", "比较路由 / 并发 / 质量变化"),
        (800, "③ 三层动态更新", "补齐状态链后测新鲜度与恢复"),
    ]
    for x, title, body in stages:
        d.box(x, 698, 356, 76, white, GREEN)
        d.text(title, x+15, 710, 16, GREEN, True)
        d.text(body, x+15, 743, 11.5, MUTED)
    d.text("判断边界：主干可进入基线测试 ≠ 企业全链路已验收；三个集合共用资源，也不保证冷热延时天然不同。", 34, 795, 11.3, INK, True)
    d.c.showPage()
    d.c.save()


if __name__ == "__main__":
    source = ROOT / "output/tiered-recall-readiness-2026-10-15/verification.json"
    target = ROOT / "output/pdf/NewsAgent_召回延时测试前状态核查_2026-10-15.pdf"
    render(json.loads(source.read_text(encoding="utf-8")), target)
    print(target)
