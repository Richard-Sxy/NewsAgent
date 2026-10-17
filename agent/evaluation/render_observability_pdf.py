"""One-page Chinese map; run with bundled reportlab, no production dependencies."""
from pathlib import Path

from render_real_news_warehouse_pdf import (
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, white, HexColor,
)


def render(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    d = Diagram(output)
    d.c.setTitle("NewsAgent 全链路可观测层与模型故障定位")
    d.c.setFillColor(HexColor("#FAFCFD"))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / OBSERVABILITY / 2026-10-16", 32, 24, 10, GREEN, True)
    d.text("从一次请求，定位到模型出错的具体阶段", 32, 48, 28, INK, True)
    d.text("统一 Trace ID 关联日志、指标与调用链；保留版本和每次尝试，不采集模型文本。", 32, 94, 14, MUTED)

    d.text("01  业务执行链路：每个关键边界都有 Span", 32, 135, 17, GREEN, True)
    stages = [
        ("API / SSE", "路由模板 + 状态码\n响应返回 Trace ID\n覆盖完整流式周期"),
        ("Temporal", "官方重放安全传播\nWorkflow / Activity\n每次尝试独立观测"),
        ("数据与检索", "SQL / 基线 / 排名\n正文 / 召回 / 重排\nEmbedding / 索引"),
        ("模型与校验", "Prompt与模型版本\nHTTP / JSON / Schema\n业务证据校验"),
        ("持久化与输出", "PostgreSQL / S3\nOutbox → Redis\n查询 / 聊天 / SSE"),
    ]
    for i, (title, body) in enumerate(stages):
        x = 32 + 229*i
        d.box(x, 174, 209, 124, PALE_GREEN, GREEN)
        d.text(title, x+14, 188, 16, GREEN, True)
        d.para(body, x+14, 223, 182, 11.8, 63)
        if i < 4:
            d.arrow(x+209, 237, x+225, 237)

    d.text("02  三类信号共用 OpenTelemetry / OTLP HTTP", 32, 329, 17, BLUE, True)
    signals = [
        (32, "日志 LOG", "安全结构化事件 + Trace/Span ID\n错误阶段、异常类型、固定分类", "Loki：按 Trace ID 查关联事件"),
        (422, "指标 METRIC", "调用量 / 错误率 / 延迟 / Token\n只用场景、操作等低基数维度", "Prometheus：看趋势与告警"),
        (812, "链路 TRACE", "父子调用 + 重试 + 版本摘要\n业务失败与 HTTP 200 分开判断", "Tempo：展开最早失败的阶段"),
    ]
    for x, title, body, target in signals:
        d.box(x, 368, 345, 118, PALE_BLUE, BLUE)
        d.text(title, x+15, 381, 16, BLUE, True)
        d.para(body, x+15, 413, 315, 11.7, 43)
        d.text(target, x+15, 461, 10.2, MUTED)
    d.box(32, 504, 1125, 39, white, BLUE)
    d.text("OTel SDK → Collector → Tempo / Loki / Prometheus → Grafana 看板、日志跳转、告警规则", 49, 515, 14, BLUE, True)

    d.text("03  模型故障定位：先看失败阶段，再看版本与尝试", 32, 573, 17, AMBER, True)
    diagnoses = [
        (32, "① HTTP 失败", "超时 / 连接 / 429 / 状态码\n检查网关、限流与服务可用性"),
        (322, "② JSON / Schema 失败", "200 返回但格式不合格\n检查模型路由、Prompt、截断"),
        (612, "③ 业务校验失败", "结构正确但证据或指标冲突\n检查输入证据与模型引用"),
        (902, "④ 下游失败", "模型通过，存储或发布进度失败\n检查 DB / S3 / Outbox / Redis"),
    ]
    for x, title, body in diagnoses:
        d.box(x, 611, 255, 95, PALE_AMBER, AMBER)
        d.text(title, x+12, 624, 14, AMBER, True)
        d.para(body, x+12, 656, 230, 10.8, 43)
    d.text("边界：不记录正文 / Prompt / 模型输出 / SQL参数 / 凭证 / 异常原文；ID只用于Trace关联，不作指标标签。", 32, 733, 11, MUTED)
    d.text("交付：默认关闭外部导出；本地独立栈显式启动。企业生产接入、容量/保留策略及模型质量仍需真实环境验收。", 32, 756, 11, MUTED)
    d.text("执行计划：盘点链路 → 统一 SDK → 分阶段埋点 → 采集与看板 → 故障注入和回归验证", 32, 791, 10.5, GREEN)
    d.c.showPage()
    d.c.save()


if __name__ == "__main__":
    render(Path(__file__).resolve().parents[2] / "output/pdf/newsagent-observability-2026-10-16.pdf")
