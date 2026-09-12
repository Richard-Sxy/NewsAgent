import pytest

from examples.hot_news_knowledge_demo import render_results, run


@pytest.mark.asyncio
async def test_offline_knowledge_demo_links_real_cache_by_news_id() -> None:
    results = await run(offline=True)

    assert len(results) == 5
    assert all(item.content is not None for item in results)
    assert results[0].content is not None
    assert results[0].content.news_id == "20260827A0C5VA00"
    assert all(
        related.news.news_id != item.content.news_id
        for item in results
        if item.content is not None
        for related in item.related_news
    )
    output = render_results(results)
    assert "热点新闻与关联报道" in output
    assert "AI数据中心" in output
