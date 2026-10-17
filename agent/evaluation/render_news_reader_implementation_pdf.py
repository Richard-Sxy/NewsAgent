"""One-page implementation map from the published aggregate and smoke evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.render_real_news_warehouse_pdf import (
    AMBER, BLUE, GRAY, GREEN, H, INK, MUTED, PALE_AMBER, PALE_BLUE, PALE_GREEN, W, Diagram,
)


def render(output: Path, package: Path, smoke_path: Path):
    manifest = json.loads((package / "manifest.json").read_text())
    query = json.loads((package / "query-smoke.json").read_text())
    smoke = json.loads(smoke_path.read_text())
    if (manifest["source_mode"] != "synthetic" or smoke["model_received_reader_profiles"] != 2
            or not smoke["same_ranking_and_metrics"] or query["api_status"] != 200):
        raise ValueError("implementation evidence is incomplete")
    output.parent.mkdir(parents=True, exist_ok=True)
    d = Diagram(output)
    d.c.setTitle("热点Agent单新闻访问人群分析 实现与边界")
    d.text("热点 Agent 已接入单篇新闻访问人群", 42, 30, 29, bold=True)
    d.text("腾讯新闻内部运营：发现热点 → 理解访问人群 → 提出推送主题方向 → 人工核查", 43, 75, 15, MUTED)
    d.text("本地实现与功能验证  |  2026-10-17", W - 355, 36, 12, MUTED)

    stats = [
        (f'{query["source_event_rows"]:,}', "复用的合成事件"),
        ("1,200", "合成身份，两个租户"),
        (f'{manifest["counts"]["reader_profiles"]:,}', "新闻 × 小时聚合"),
        ("20 篇 / 批", "Top100 最多五批"),
    ]
    gap, width = 18, (W - 84 - 54) / 4
    for index, (number, label) in enumerate(stats):
        x = 42 + index * (width + gap)
        d.box(x, 114, width, 76, PALE_GREEN)
        d.text(number, x + 18, 123, 24, GREEN, True)
        d.text(label, x + 18, 157, 12, MUTED)

    d.text("已实现的端到端调用链", 42, 212, 19, bold=True)
    boxes = [
        ("01  冻结模拟数仓", "公开新闻正文与模拟指标\n独立合成访问事件\n源哈希与新闻身份校验", PALE_BLUE),
        ("02  离线访客聚合", "每篇、每窗去重 detail_view\n访问前 30 天 / 24 小时兴趣\n排除当前新闻的历史贡献", PALE_GREEN),
        ("03  在线有界读取", "租户 + 新闻 + 窗口索引\n批量查询、合并并发请求\n版本校验与短时聚合缓存", PALE_BLUE),
        ("04  热点模型解释", "只接收本篇聚合画像\n长短期主题、覆盖与基准\n输出需人工审核的主题建议", PALE_GREEN),
        ("05  报告与页面", "快照保留来源、修订和目录\n展示中文标签与低样本提示\n可触达人数另经资格接口", PALE_BLUE),
    ]
    step_gap = 20
    step_width = (W - 84 - 4 * step_gap) / 5
    for index, (title, body, fill) in enumerate(boxes):
        x = 42 + index * (step_width + step_gap)
        d.box(x, 250, step_width, 142, fill)
        d.text(title, x + 13, 265, 16, bold=True)
        d.para(body, x + 13, 305, step_width - 26, 12, maxh=78)
        if index < 4:
            d.arrow(x + step_width + 3, 323, x + step_width + step_gap - 3, 323)

    panels = [
        ("口径保证", [
            "人数、占比、基线与热度由 Python / SQL 计算。",
            "未知画像保留在全体读者分母；冷启动单独说明。",
            "多标签可以重叠，不能相加推算可触达人数。",
            "早期历史不足会提示；缺失数据不会冒充零。",
        ], PALE_GREEN, GREEN),
        ("延时治理", [
            "离线承担事件去重、画像关联与标签聚合。",
            "在线只读聚合；每批默认 3 秒、全批 5 秒。",
            "缓存默认 30 秒；16 个不同在途请求上限。",
            f'本机 20 篇批读 P95：{query["uncached_local_aggregate_port"]["p95_ms"]:.1f} ms。',
        ], PALE_BLUE, BLUE),
        ("验证范围与剩余工作", [
            "两条真实正文的画像已实际进入本地模型。",
            "样例每篇只有 1 位合成读者，已标低支持。",
            "已验证热点排名与指标保持一致、API 鉴权。",
            "企业接口、模型质量、千万级 SLA 尚待验收。",
        ], PALE_AMBER, AMBER),
    ]
    panel_gap = 18
    panel_width = (W - 84 - panel_gap * 2) / 3
    for index, (title, bullets, fill, color) in enumerate(panels):
        x = 42 + index * (panel_width + panel_gap)
        d.box(x, 420, panel_width, 208, fill)
        d.text(title, x + 17, 436, 19, color, True)
        for row, body in enumerate(bullets):
            d.text(f"{row + 1}", x + 17, 479 + row * 32, 12, color, True)
            d.para(body, x + 38, 478 + row * 32, panel_width - 55, 11.5, maxh=31)

    d.box(42, 650, W - 84, 117, PALE_AMBER)
    d.text("企业落地边界", 59, 664, 18, AMBER, True)
    d.para("企业数仓保留个人明细与身份映射 → 企业侧预聚合 → 固定 HTTPS 聚合 Port → NewsAgent 分析与人工决策。\n"
        "本地 P95 仅是小规模聚合读取探针，不包含网络、模型或生产并发；当前不触发真实推送。\n"
        "动态 Schedule 的画像冻结仍待接入，当前明确拒绝；已有生产 Bundle 不会自动升级。\n"
        "已提供兼容 corpus-v6 的页面配置，整栈页面待启动确认；v8 在线 SQL 场景仍待注册。",
        59, 694, W - 118, 11.5, maxh=70)
    d.text("实现：app/reader_profiles  +  HotNewsEnrichmentService  +  v3 Prompt  +  NewsReaderProfilePanel", 42, 784, 11, MUTED)
    d.text("证据：聚合包 manifest / query-smoke.json / local-readers-v1-20261017-smoke.json  |  1 / 1", 42, 807, 10, MUTED)
    d.c.showPage()
    d.c.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--smoke", type=Path, required=True)
    args = parser.parse_args()
    render(args.output, args.package, args.smoke)


if __name__ == "__main__":
    main()
