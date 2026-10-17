"""Single-page Chinese map generated from the built behavior package and test evidence."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent/evaluation"))
from render_real_news_warehouse_pdf import Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER, PALE_GREEN, PALE_BLUE, PALE_AMBER, GRAY
from reportlab.lib.colors import HexColor, white


def render():
    package = ROOT / "output/behavior-tags/local-merged-v1-20261016"
    manifest = json.loads((package / "manifest.json").read_text())
    report = json.loads((package.parent / "verification-20261016.json").read_text())
    counts = manifest["counts"]
    output = ROOT / "output/pdf/newsagent-local-behavior-tags-2026-10-16.pdf"
    d = Diagram(output)
    d.c.setTitle("NewsAgent 多版本数仓、行为标签与热点检索")
    d.c.setFillColor(HexColor("#F9FCFD")); d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / LOCAL BEHAVIOR TAGS / 2026.10.16", 32, 23, 10, GREEN, True)
    d.text("多版本数仓 → 行为标签 → 热点相关新闻检索", 32, 48, 27, INK, True)
    d.text("腾讯新闻内部运营场景：看热点、理解阅读兴趣、检索相关报道、准备人工决策", 32, 91, 13, MUTED)
    d.box(32, 120, 1125, 36, PALE_AMBER)
    d.text("公开新闻正文 + 模拟聚合指标；个人事件与画像为独立合成，无法从聚合数据还原真实个人。", 46, 129, 12, AMBER)
    cards = [(32, "1 统一多版本输入", PALE_BLUE, BLUE), (320, "2 建立可解释标签", PALE_GREEN, GREEN),
             (608, "3 生成本地画像", PALE_BLUE, BLUE), (896, "4 接入热点检索", PALE_GREEN, GREEN)]
    for x, title, fill, color in cards:
        d.box(x, 183, 261, 236, fill, color)
        d.text(title, x+16, 199, 18, color, True)
    d.para(f"{report['source_count']} 个来源统一装载\nv1-v6 版本化生成器\nv7 / v8 冻结 JSONL\n\n哈希校验、内容去重\n撤回/受限内容统一排除\n场景、指标口径分别保留", 48, 240, 225, 12, 167)
    d.para(f"12 个主题 / 72 个细分\n{counts['content_labels']:,} 条内容标签\n{counts['news_behavior_labels']:,} 条窗口标签\n\n点击、深读、增长、互动\n负反馈、小样本、缺基线\n保留命中词、公式和阈值", 336, 240, 225, 12, 167)
    users = manifest['users_per_tenant'] * len(manifest['tenants'])
    d.para(f"{users:,} 个合成用户 / 10 类场景\n{counts['synthetic_events']:,} 条合成事件\n{counts['synthetic_profiles']:,} 条每日画像\n\n深读 / 快览 / 跨主题\n冷启动 / 兴趣迁移 / 负反馈\n渠道、设备、活跃时段", 624, 240, 225, 12, 167)
    d.para("已有召回与生命周期门禁\n→ 原相关性过滤与评分\n→ 共享主题兴趣加分\n→ 分析证据与工作台详情\n\n加分上限 0.08，保留主体约束\n包摘要绑定运行幂等键\n热点分数保持原算法", 912, 240, 225, 12, 167)
    for a, b in [(293, 313), (581, 601), (869, 889)]:
        d.arrow(a, 293, b, 293)
    d.box(32, 446, 685, 191, white, BLUE)
    d.text("标签实例：新闻主题与消费特征分层记录", 49, 461, 18, BLUE, True)
    d.para("科技 → 自然 / 航天 / 芯片 / 机器人    AI → 模型 / 应用 / 算力 / 治理\n财经 → 宏观 / 市场 / 企业    教育 → 高校 / 职教 / 基础教育\n体育、汽车、娱乐、社会、国际、健康、文化等为草案扩展", 49, 502, 650, 12, 70)
    d.para("主题标签记录文本命中；行为标签记录单篇单小时指标。原 UV 不跨新闻、渠道或小时相加。\n缺失字段保留为空，既有零基线与缺失基线分开；无依据的细分主题可保持零覆盖。", 49, 579, 650, 11, 49, MUTED)
    d.box(742, 446, 415, 191, white, GREEN)
    d.text("功能验证与数据边界", 759, 461, 18, GREEN, True)
    d.para(f"{report['tests_passed']} 项专项与相关回归通过\n实际重排顺序变化，低相关候选未复活\n跨租户、输入摘要、未来快照隔离\nSQLite 完整性、事件时序已检查", 759, 502, 380, 12, 87)
    d.text("功能验证不代表推荐效果或企业模型质量验收", 759, 604, 10.5, MUTED)
    d.box(32, 662, 1125, 111, PALE_AMBER)
    d.text("现在可用", 49, 676, 15, AMBER, True)
    d.para("离线构建 / CLI 检索 / 只读标签 API / 手工热点运行兴趣重排 / 合成受众预览。API 与 Worker 读取同一份只读包。", 49, 706, 1080, 12, 25)
    d.para("待企业落地：正式目录审批、授权聚合画像接口及推荐效果评测。当前不执行真实推送；定时 Schedule 的动态画像冻结仍待接入。", 49, 739, 1080, 10.8, 23, MUTED)
    d.text("产物：output/behavior-tags/local-merged-v1-20261016  |  调用链与配置：agent/docs/local-behavior-tags.md", 32, 795, 10, MUTED)
    d.c.showPage(); d.c.save()
    print(output)


if __name__ == "__main__":
    render()
