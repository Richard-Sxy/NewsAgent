"""One-page proposed middleware map for the stable first tiered-recall release.

Chain: this CLI -> Diagram/ReportLab -> Chinese PDF -> pypdf/Poppler QA.
Config: A3 landscape, embedded STHeiti fonts, fixed output path.
Sources: project architecture and Milvus integration contract; official Milvus
and Temporal docs. This artifact does not change runtime configuration.
TODO: enterprise contracts, durable index jobs, cross-tier version gate,
migration recovery, and a 48-hour controlled acceptance run.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent/evaluation"))
from render_real_news_warehouse_pdf import (  # noqa: E402
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, white, HexColor,
)


def render(output: Path) -> None:
    d = Diagram(output)
    d.c.setTitle("NewsAgent - 第一版分层召回企业中间件接入")
    d.c.setFillColor(HexColor("#FAFCFD"))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / STABLE FIRST RELEASE / 2026-10-15", 32, 22, 10, GREEN, True)
    d.text("第一版：复用企业中间件，把三层召回跑稳", 32, 43, 28, INK, True)
    d.text("Python API + 索引Worker；三层先放同一受管向量服务，限定首批万级数据，先验收内容正确与故障恢复。", 32, 90, 12.4, MUTED)
    d.box(32, 119, 1125, 42, PALE_AMBER)
    d.text("这是接入建议：企业具体服务与配额待确认；现有Milvus查询已接线，增量写入/更正/撤稿同步尚需补齐。", 46, 132, 11.5, AMBER, True)

    d.text("入库与变更：先持久保存任务，失败后可恢复", 32, 180, 16, INK, True)
    boxes = (
        (32, 184, "企业内容接口", "正文 / 发布时间 / 版本\n撤稿状态 / 增量游标"),
        (250, 209, "PostgreSQL", "文档与版本台账\n同一事务写待处理任务"),
        (493, 210, "Temporal + Worker", "复用现有调度\n切片 / 限流 / 有界重试"),
        (737, 192, "统一Embedding", "固定模型版本与维度\n批量生成 / 校验向量"),
        (963, 194, "受管向量服务", "优先现有Milvus适配\n写入 / 验证 / 删除"),
    )
    for x, width, title, body in boxes:
        d.box(x, 215, width, 91, PALE_BLUE, BLUE)
        d.text(title, x+13, 227, 14, BLUE, True)
        d.para(body, x+13, 258, width-26, 10.5, 35)
    for x1, x2 in ((216, 243), (459, 486), (703, 730), (929, 956)):
        d.arrow(x1, 262, x2, 262, BLUE)
    d.route([(1062, 306), (1062, 329), (355, 329), (355, 306)], GREEN)
    d.text("验证目标版本可检索 → 提交有效版本/所在层 → 清理旧投影；迁移失败保留源层，任务和进度留在数据库。", 367, 313, 9.5, GREEN)

    d.box(32, 349, 545, 137, PALE_GREEN, GREEN)
    d.text("一个向量服务，三个逻辑集合", 48, 361, 16, GREEN, True)
    for x, title, body in (
        (48, "HOT", "新稿 / 活跃内容\n优先处理变更"),
        (221, "WARM", "降温内容\n定时迁移与对账"),
        (394, "COLD", "历史内容\n回溯与旧闻回热"),
    ):
        d.box(x, 397, 155, 64, white, GREEN)
        d.text(title, x+10, 403, 12, GREEN, True)
        d.para(body, x+10, 425, 137, 9.5, 29)
    d.text("首版统一模型与索引类型，预加载验收集合；三集合本身不会保证冷热延时差异。", 48, 470, 9.5, MUTED)

    d.box(607, 349, 550, 137, PALE_BLUE, BLUE)
    d.text("查询：身份可信，证据返回前复核", 623, 361, 16, BLUE, True)
    d.para("企业网关 → Python API → 同版本Query Embedding\n→ 三层并发召回 → 按PG版本/撤稿状态过滤\n→ 每新闻仅一次贡献与输出 → 融合结果 / 证据\n统一总超时、限制候选量和并发；PG不可用时停止输出证据。", 623, 400, 516, 11, 75)

    d.text("稳定运行的关键：撤稿与更正跨三层生效，不能只等向量物理删除", 32, 507, 16, INK, True)
    cards = (
        (32, "新增积压", "任务落库后处理\n有效存量仍可查询"),
        (318, "内容更正", "旧版先失效\n新版可检索后放行"),
        (604, "撤稿", "台账先屏蔽所有出口\n异步删除三层旧切片"),
        (890, "失败 / 乱序 / 重复", "按版本拒绝旧事件\n幂等恢复，失败告警"),
    )
    for x, title, body in cards:
        d.box(x, 545, 267, 82, white, GREEN)
        d.text(title, x+13, 555, 13, GREEN, True)
        d.para(body, x+13, 583, 241, 10.5, 33)

    d.box(32, 649, 545, 100, PALE_BLUE, BLUE)
    d.text("向企业平台申请的接入资料", 48, 660, 15, BLUE, True)
    d.para("内容API与更新/撤稿合同；Embedding模型、维度、限额。\nPG库/账号/备份；向量地址、三集合、权限、一致性和配额。\nTemporal命名空间/任务队列；部署、网关、密钥、日志与监控入口。", 48, 693, 512, 10.5, 48)
    d.box(607, 649, 550, 100, PALE_AMBER, AMBER)
    d.text("复用项目现有依赖，首版少加系统", 623, 660, 15, AMBER, True)
    d.para("Redis继续服务现有缓存/SSE；S3继续保存报告与评测资产。\n索引任务状态以PG为准，调度复用Temporal；无需新增专属MQ。\n日志/指标/告警接企业现有平台；物理冷热分离与压缩后续再评估。", 623, 693, 516, 10.5, 48)

    d.text("建议验收：连续48小时 + 杀Worker/断Embedding/断向量服务 + 新增/更正/撤稿/迁移/重复事件；恢复后任务不丢。", 32, 769, 10.5, INK, True)
    d.text("监控：任务积压与最老等待、更新可见延时、撤稿残留、各层p95/失败率、连接池与资源；延时阈值由实测约定。", 32, 791, 10.1, MUTED)
    d.text("待补：持久化索引工作流、跨层版本/撤稿Gate、迁移恢复、查询路径统一及企业合同验收；本图未修改运行配置。", 32, 812, 9.8, MUTED)
    d.c.linkURL("https://milvus.io/docs/consistency.md", (32, 5, 185, 20), relative=0)
    d.c.linkURL("https://milvus.io/docs/load-and-release.md", (195, 5, 400, 20), relative=0)
    d.c.linkURL("https://docs.temporal.io/encyclopedia/retry-policies", (410, 5, 620, 20), relative=0)
    d.c.showPage()
    d.c.save()


if __name__ == "__main__":
    output = ROOT / "output/pdf/NewsAgent_第一版分层召回企业中间件接入_2026-10-15.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    render(output)
    print(output)
