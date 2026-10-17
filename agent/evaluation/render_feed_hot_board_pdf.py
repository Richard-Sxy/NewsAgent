"""A one-page Chinese concept map: public evidence vs proposed Feed design."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from render_real_news_warehouse_pdf import (
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, GRAY, white, HexColor,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "agent/data/real-news-warehouse-v8/catalog.json"
CATALOG_SHA = "1af51f4fba5f190829d255fe8ee2ce2328811f09102af6d9e814e2b49d4353f0"


def real_titles() -> list[str]:
    raw = CATALOG.read_bytes()
    if sha256(raw).hexdigest() != CATALOG_SHA:
        raise ValueError("diagram requires the frozen real-news catalog")
    articles = json.loads(raw)["articles"]
    ids = ("20260903A05YQE00", "20260903A06XH100", "20260903A06YIL00")
    mapped = {row["news_id"]: row for row in articles}
    if any(mapped[news_id]["news_content_synthetic"] is not False for news_id in ids):
        raise ValueError("Feed illustration must use real cached headlines")
    return [mapped[news_id]["title"] for news_id in ids]


def phone(d, x, y, titles):
    d.box(x+5, y+5, 162, 237, GRAY)
    d.box(x, y, 162, 237, INK)
    d.box(x+7, y+13, 148, 210, white)
    d.text("推荐 Feed", x+19, y+25, 13, BLUE, True)
    for i, title in enumerate(titles):
        cy = y+55+i*49
        d.box(x+16, cy, 129, 42, PALE_BLUE)
        d.para(title, x+22, cy+6, 117, 9, 31)
    d.text("真实标题 · 顺序仅为示意", x+18, y+207, 8.8, MUTED)


def render(output: Path) -> None:
    titles = real_titles()
    d = Diagram(output)
    d.c.setTitle("NewsAgent - Feed流、热点榜与数仓关系")
    d.c.setSubject("公开历史榜单、Feed内容来源和拟议分发统计闭环；未部署Feed")
    d.c.setFillColor(HexColor("#FAFCFD"))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / FEED & RANKING / 2026-10-15", 32, 25, 10, GREEN, True)
    d.text("Feed 是内容流，热点榜可以是其中一路内容来源", 32, 48, 27, INK, True)
    d.text("榜单：在指定范围、窗口和规则下排序   |   Feed：组织并交付一串内容卡片", 32, 95, 13.5, MUTED)
    d.box(32, 122, 1125, 29, PALE_AMBER)
    d.text("公开历史名称与当前内部清单分开；下图Feed及新增榜单为设计建议，现有v8尚未运行个性化Feed。", 44, 130, 10.8, AMBER)

    d.box(32, 174, 545, 121, PALE_BLUE, BLUE)
    d.text("公开历史介绍：2020年腾讯ConTech", 48, 187, 16, BLUE, True)
    d.text("热点榜 · 较真榜 · 互动榜 · 热点精选", 48, 220, 18, INK, True)
    d.para("来源为当时公开演讲报道；当前是否保留及具体权重未确认。\n热点精选/早晚报/频道是内容组织形式，不必全是数值排行榜。", 48, 253, 512, 10.4, 33, MUTED)

    d.box(607, 174, 550, 121, PALE_GREEN, GREEN)
    d.text("适合本项目模拟的6类榜/运营列表", 623, 187, 16, GREEN, True)
    d.text("综合热点 · 频道热点 · 快速上升", 623, 220, 17, INK, True)
    d.text("深度阅读 · 互动讨论 · 运营巡查", 623, 250, 17, INK, True)
    d.text("以上是拟议设计，不是腾讯当前内部榜单清单。", 623, 276, 9.8, MUTED)

    d.text("拟议闭环：榜单提供候选，Feed消费又形成新的统计", 32, 318, 17, INK, True)

    d.box(32, 367, 155, 119, PALE_GREEN, GREEN)
    d.text("真实内容库", 45, 381, 15, GREEN, True)
    d.para("news_id + 正文 + 版本\n共享资格Gate\n审核/状态/撤稿屏蔽", 45, 413, 129, 10.7, 55)
    d.arrow(187, 426, 211, 426, BLUE, True)

    d.box(222, 367, 164, 119, PALE_BLUE, BLUE, True)
    d.text("其他内容来源", 235, 381, 14, BLUE, True)
    d.para("兴趣 / 关注\n新鲜内容 / 频道\n编辑选择 / 重要资讯", 235, 413, 138, 10.7, 55)
    d.route([(386, 393), (398, 393), (398, 350), (751, 350), (751, 367)], BLUE)

    d.box(415, 367, 183, 119, PALE_BLUE, BLUE, True)
    d.text("热点候选 / 热度榜", 428, 381, 13.5, BLUE, True)
    d.para("群体/频道/地域范围\n小时窗口 + 版本规则\n保留原榜rank和位置", 428, 413, 157, 10.7, 55)
    d.arrow(598, 426, 630, 426, BLUE, True)

    d.box(641, 367, 226, 119, PALE_BLUE, BLUE, True)
    d.text("推荐 Feed 的选择过程", 655, 381, 14, BLUE, True)
    d.para("多路召回 → 排序 → 重排\n兴趣、质量、时效和多样性\n粗排/精排可按容量进一步拆分", 655, 413, 199, 10.7, 55)
    d.arrow(867, 426, 977, 426, BLUE, True)
    d.text("卡片交付", 887, 406, 10.2, MUTED)
    phone(d, 985, 345, titles)

    d.box(32, 567, 309, 86, PALE_GREEN, GREEN)
    d.text("曝光 → 点击 → 阅读 / 互动 / 负反馈", 45, 580, 12.5, GREEN, True)
    d.para("有效曝光与点击归因需要版本合同；\n卡片交付、页面渲染、用户有效看到分开。", 45, 611, 282, 10.2, 32)
    d.arrow(341, 611, 430, 611, GREEN)
    d.text("企业端聚合", 348, 584, 9.8, MUTED)

    d.box(440, 567, 217, 86, PALE_GREEN, GREEN)
    d.text("数仓 / 指标 / 规则快照", 453, 580, 13.5, GREEN, True)
    d.para("当前v8有五分钟、小时、日表\nFeed策略/位置/实验维度尚缺", 453, 611, 191, 10.2, 32)
    d.route([(509, 567), (509, 486)], GREEN)
    d.text("更新榜单", 520, 522, 10.5, GREEN)
    d.route([(657, 611), (753, 611), (753, 486)], BLUE)
    d.text("特征 / 效果评估", 765, 545, 10.3, BLUE)
    d.route([(1147, 522), (1166, 522), (1166, 670), (186, 670), (186, 653)], GREEN)

    d.box(32, 699, 359, 82, white, BLUE)
    d.text("榜单rank ≠ Feed位置", 46, 710, 13.5, BLUE, True)
    d.para("同一热门新闻可在不同推荐流处于不同位置；\n榜外的小众内容也可以适合某次访问。", 46, 738, 331, 10.2, 31)
    d.box(414, 699, 359, 82, white, GREEN)
    d.text("分析Feed效果，要补上下文", 428, 710, 13.5, GREEN, True)
    d.para("策略/模型版本、实验桶、位置组、有效曝光、归因；\n本地仅接聚合，不保留企业用户身份明细。", 428, 738, 331, 10.2, 31)
    d.box(796, 699, 361, 82, PALE_AMBER, AMBER)
    d.text("撤稿共用资格Gate，所有出口复核", 810, 710, 13.1, AMBER, True)
    d.para("Feed/缓存、榜单、向量检索共同屏蔽；\n物理清理异步执行，失败不能解除屏蔽。", 810, 738, 333, 10.2, 31)

    d.text("点击也受展示位置与人群选择影响；CTR不自动消除偏置。UV不能跨新闻/渠道/实验桶直接相加。", 32, 792, 10, MUTED)
    d.text("参考：2020公开演讲 · 2025腾讯新闻插件技术案例 · Google推荐流程  |  蓝色虚线框为拟议Feed能力", 32, 814, 9.2, MUTED)
    d.c.linkURL("https://cloud.tencent.com/developer/news/735540", (32, 8, 240, 28), relative=0)
    d.c.linkURL("https://cloud.tencent.com/developer/article/2483974", (250, 8, 485, 28), relative=0)
    d.c.linkURL("https://developers.google.com/machine-learning/recommendation/overview/types", (495, 8, 655, 28), relative=0)
    d.c.showPage()
    d.c.save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    render(args.output)
    print(args.output.absolute())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
