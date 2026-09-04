"""运行热点排行、知识库重排和规则分析 Agent 的完整演示。"""

from __future__ import annotations

import argparse
import asyncio

from app.agents.hot_news_analysis import RuleBasedHotNewsAnalyzer, build_analysis_input
from examples.hot_news_knowledge_demo import run


def render_analysis(results) -> str:
    analyzer = RuleBasedHotNewsAnalyzer()
    lines = ["热点新闻 Agent 分析", "=" * 60]
    for item in results:
        if item.content is None:
            continue
        analysis = analyzer.analyze(build_analysis_input(item))
        lines.append(f"[{analysis.news_id}] {analysis.trend_summary}")
        lines.append(f"  关注原因：{'；'.join(analysis.attention_reasons)}")
        lines.append(
            f"  关联背景：{'；'.join(analysis.related_context) or '无可靠关联报道'}"
        )
        lines.append(f"  运营建议：{'；'.join(analysis.operation_suggestions)}")
        lines.append(
            f"  证据新闻：{', '.join(analysis.evidence_news_ids) or '无'}"
        )
    return "\n".join(lines)


async def main_async(*, offline: bool) -> None:
    print(render_analysis(await run(offline=offline)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="不请求 FastGPT")
    args = parser.parse_args()
    asyncio.run(main_async(offline=args.offline))


if __name__ == "__main__":
    main()
