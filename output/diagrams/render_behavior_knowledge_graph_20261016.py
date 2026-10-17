"""Render a one-page entity/relation map from the verified behavior-tag package.

Entry: this script. Inputs: immutable Manifest, compact verification and runtime
example JSON. Transform: checked counts + curated entity/relation coordinates.
Core: render()/node()/edge(). Dependencies: ReportLab, existing Diagram helper,
local Chinese fonts; no network/model calls. Output: a new PDF, no data mutation.
Config: ROOT/OUTPUT constants. Check: pdfinfo + pdftoppm and visual inspection.
TODO: production profile integration and graph storage are outside this drawing.
"""

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "output/pdf/newsagent-behavior-knowledge-graph-2026-10-16.pdf"
sys.path.insert(0, str(ROOT / "agent/evaluation"))
from render_real_news_warehouse_pdf import (
    Diagram, W, H, INK, MUTED, BLUE, GREEN, AMBER, GRAY,
    PALE_BLUE, PALE_GREEN, PALE_AMBER,
)
from reportlab.lib.colors import HexColor, white

PURPLE, PALE_PURPLE = HexColor("#7757A8"), HexColor("#F2EDF9")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def render():
    directory = ROOT / "output/behavior-tags"
    package = directory / "local-merged-v1-20261016"
    payload = (package / "manifest.json").read_bytes()
    manifest = json.loads(payload)
    report = json.loads((directory / "verification-20261016.json").read_text())
    example = json.loads((directory / "runtime-example-20261016.json").read_text())
    identity = dict(manifest)
    declared = identity.pop("identity_sha256")
    if hashlib.sha256(canonical(identity)).hexdigest() != declared:
        raise ValueError("manifest identity mismatch")
    package_hash = hashlib.sha256(payload).hexdigest()
    if report["status"] != "passed" or report["manifest_identity_sha256"] != declared:
        raise ValueError("knowledge graph requires a matching verified package")
    if report["package_sha256"] != package_hash or example["package_sha256"] != package_hash:
        raise ValueError("runtime/verification package mismatch")
    if report["counts"] != manifest["counts"]:
        raise ValueError("graph counts mismatch")
    topics = report["topic_coverage"]
    roots = [item for item in topics if item["parent_topic_id"] is None]
    leaves = [item for item in topics if item["parent_topic_id"] is not None]
    label_ids = {item["label_id"] for item in report["news_behavior_label_dictionary"]}
    selected = next(item for item in report["search_examples"]["tech"]
        if item["content"]["news_id"] == "20261014A0BCJO00")
    article, observation = selected["content"], selected["behavior"]
    assert {"tech", "tech.robotics", "ai", "ai.ecosystem"} <= set(article["topic_ids"])
    assert {"reach.high_impressions", "reach.high_clicks", "baseline.missing"} <= set(observation["label_ids"])
    assert observation["baseline_clicks"] is None
    bonus = Decimal("0.08") * Decimal(str(example["topic_weights"]["tech"]))
    d = Diagram(OUTPUT)
    d.c.setTitle("NewsAgent 行为标签知识图谱 - 已构建实体、关系与检索用途")
    d.c.setFillColor(HexColor("#FAFCFD")); d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / BEHAVIOR KNOWLEDGE GRAPH / 2026.10.16", 32, 22, 10, GREEN, True)
    d.text("行为标签已构建：一张图看懂实体与关系", 32, 46, 28, INK, True)
    d.text("基于已生成数据包的关系图；新闻窗口标签、合成用户画像分别建模，再关联到热点检索。", 32, 90, 12, MUTED)

    counts = manifest["counts"]
    stats = [(f"{len(manifest['source_identity']['sources'])} 个来源", "多版本分场景保留"),
        (f"{len(roots)} / {len(leaves)}", "主题 / 细分目录"),
        (f"{counts['content_labels']:,}", "内容主题记录"),
        (f"{counts['news_behavior_labels']:,}", "新闻小时行为记录"),
        (f"{report['synthetic_user_count']:,} / {counts['synthetic_profiles']:,}", "合成用户 / 每日画像")]
    for i, (number, label) in enumerate(stats):
        x = 32+i*228
        d.box(x, 119, 213, 54, white, GRAY)
        d.text(number, x+13, 124, 19, GREEN, True)
        d.text(label, x+13, 151, 10.4, MUTED)

    # Relationship edges are rendered first; cards provide clean edge endpoints.
    def edge(points, label, lx, ly, color=GREEN, size=10):
        if len(points) == 2:
            d.arrow(*points[0], *points[1], color)
        else:
            d.route(points, color)
        # Light background keeps edge predicates readable at crossings.
        width = max(30, len(label)*size+10)
        d.box(lx-4, ly-1, width, size+7, HexColor("#FAFCFD"))
        d.text(label, lx, ly, size, color)

    def node(x, y, w, h, title, subtitle, fill, color, body_size=11):
        d.box(x, y, w, h, fill, color)
        d.text(title, x+14, y+11, 15, color, True)
        d.para(subtitle, x+14, y+36, w-28, body_size, maxh=h-40, color=INK)

    edge([(300, 255), (345, 255)], "产出", 304, 232, BLUE)
    edge([(345, 275), (322, 275), (322, 408), (300, 408)], "按新闻ID绑定", 202, 321, GREEN)
    edge([(480, 298), (480, 368)], "规则命中 / 多标签", 493, 328, AMBER)
    edge([(177, 452), (177, 490)], "指标阈值计算", 190, 464, GREEN)
    edge([(955, 264), (955, 305)], "产生", 968, 278, PURPLE)
    edge([(805, 339), (660, 339), (660, 256), (615, 256)], "访问对象", 672, 307, PURPLE)
    edge([(955, 377), (955, 438)], "仅聚合截止前事件", 963, 399, PURPLE)
    edge([(805, 466), (705, 466), (705, 410), (615, 410)], "偏好主题 + 权重", 674, 382, PURPLE)
    edge([(480, 452), (480, 490)], "共享主题", 493, 463, AMBER)
    edge([(300, 526), (345, 526)], "筛选", 304, 504, GREEN)
    edge([(805, 495), (739, 495), (739, 521), (615, 521)], "聚合兴趣加分", 645, 502, PURPLE)
    edge([(615, 550), (805, 550)], "选中证据与排序理由", 634, 555, GREEN, 9.4)

    node(55, 215, 245, 83, "版本化数仓来源", "v1-v6 生成配置 + v7/v8 冻结表\n按租户、版本、场景、指标口径隔离", PALE_BLUE, BLUE, 10.5)
    node(345, 215, 270, 83, "新闻 News", "news_id / 标题 / 正文哈希 / 来源\n包含公开内容与有标记的合成内容", PALE_BLUE, BLUE, 10.7)
    node(55, 368, 245, 84, "新闻小时快照", "曝光 / 点击 / 有效消费 / 互动\n绑定时间、修订、水位与基线", PALE_GREEN, GREEN)
    node(345, 368, 270, 84, "主题标签 Topic", "科技 → 机器人；AI → 生态\n保留命中词、父主题及规则版本", PALE_AMBER, AMBER)
    node(55, 490, 245, 74, f"新闻行为标签 · {len(label_ids)} 类", "高点击 / 高负反馈 / 增长 / 缺基线等\n每项保留指标、公式与阈值", PALE_GREEN, GREEN, 10.4)
    node(345, 490, 270, 74, "标签检索 / 热点重排", "原相关性门槛 → 共享主题兴趣加分\n单条加分 ≤ 0.08；保留主体约束", PALE_GREEN, GREEN, 10.5)
    node(805, 207, 300, 57, "合成用户 Synthetic User", "1,200 人；10 类预置阅读场景", PALE_PURPLE, PURPLE, 10.6)
    node(805, 305, 300, 72, "合成行为 Event", f"{counts['synthetic_events']:,} 条曝光 / 点击 / 阅读 / 互动\n附渠道、设备、时间与内容关联", PALE_PURPLE, PURPLE, 10.5)
    node(805, 438, 300, 72, "每日兴趣画像 Profile", "主题权重 / 深浅阅读 / 时段 / 负反馈\n按 as_of 冻结，兴趣分按时间衰减", PALE_PURPLE, PURPLE, 10.5)
    d.box(805, 533, 300, 37, PALE_GREEN, GREEN)
    d.text("分析报告 / 运营工作台", 821, 541, 14, GREEN, True)

    # Compact details complement the graph, rather than replacing its edges.
    for x, w, color in [(32, 361, BLUE), (410, 475, GREEN), (902, 255, PURPLE)]:
        d.box(x, 599, w, 170, white, color)
    d.text("目录分支（示例）", 47, 611, 15, BLUE, True)
    d.para("科技 → 自然 / 芯片 / 机器人\nAI → 大模型 / 算力 / 应用\n财经 → 宏观 / 公司 / 金融市场\n教育 → 基础 / 高等 / 职业教育", 47, 643, 330, 11.5, 72)
    d.para("其余主题：军事、体育、汽车、娱乐、社会、国际、健康、文化。84 项中 83 项有覆盖；军史保持空白。", 47, 723, 330, 10, 37, MUTED)

    d.text("已落盘的一条新闻关系实例", 425, 611, 15, GREEN, True)
    d.para(article["title"], 425, 642, 443, 12, 22)
    d.text("机器人 tech.robotics → 科技 tech；AI 生态 → AI", 425, 671, 10.5, INK)
    d.text(f"10月14日 19-20时：曝光 {observation['impressions']:,}，点击 {observation['clicks']:,}（模拟指标）", 425, 693, 10.4, INK)
    d.text("→ 高曝光 / 高点击 / 缺基线；缺基线保留为空", 425, 715, 10.4, INK)
    d.text(f"仅共享科技时：0.08 × {example['topic_weights']['tech']:.4f} ≈ {bonus:.4f}（加分示例）", 425, 740, 10.2, MUTED)

    d.text("完成状态与边界", 917, 611, 15, PURPLE, True)
    d.para("已构建：标签、事件、画像、索引\n已接代码：只读查询与热点重排\n已验证：393 项功能与相关回归\n\n个人画像与事件均为合成\n原 UV 不跨窗口累加\n既有在线实例未重启", 917, 642, 225, 10.6, 119)
    d.text("图例：蓝=内容与来源   绿=聚合指标与检索   紫=合成用户链路   黄=主题关系", 32, 787, 10, MUTED)
    d.text("实体关系映射现有 JSONL / SQLite；本次绘图未引入图数据库。企业画像接口、Schedule 动态冻结与真实推送尚待接入。", 32, 807, 9.7, MUTED)
    d.c.showPage(); d.c.save()
    print(OUTPUT)


if __name__ == "__main__":
    render()
