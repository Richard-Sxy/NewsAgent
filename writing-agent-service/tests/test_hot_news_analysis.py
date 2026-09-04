import pytest

from app.agents.hot_news_analysis import RuleBasedHotNewsAnalyzer, build_analysis_input
from examples.hot_news_knowledge_demo import run


@pytest.mark.asyncio
async def test_builds_analysis_from_existing_hot_news_chain() -> None:
    enriched = await run(offline=True)
    item = enriched[0]

    input_data = build_analysis_input(item)
    result = RuleBasedHotNewsAnalyzer().analyze(input_data)

    assert result.news_id == item.ranking.current.news_id
    assert result.trend_summary
    assert result.attention_reasons
    assert result.operation_suggestions
    assert result.evidence_news_ids == tuple(
        related.news.news_id
        for related in item.related_news
        if related.news.news_id
    )


def test_rejects_analysis_without_content() -> None:
    # 该异常分支由适配函数负责，分析器始终只接收完整的结构化输入。
    from app.analytics.hot_news_enrichment import EnrichedHotNews
    from examples.hot_news_demo import build_ranking, load_scenario

    item = EnrichedHotNews(
        ranking=build_ranking(load_scenario(), limit=1)[0],
        content=None,
        related_news=(),
    )

    with pytest.raises(ValueError, match="without content"):
        build_analysis_input(item)
