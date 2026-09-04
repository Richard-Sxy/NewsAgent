from examples.hot_news_demo import build_ranking, load_scenario, render_ranking

"""加载示例场景，构建新闻排行榜，并渲染输出"""
def test_demo_builds_expected_hot_news_ranking() -> None:
    scenario = load_scenario()
    ranked = build_ranking(scenario)

    assert [item.current.news_id for item in ranked] == [
        "20260827A0C5VA00",
        "20260828A009SN00",
        "20260831A09TNH00",
    ]
    assert [item.rank for item in ranked] == [1, 2, 3]
    assert ranked[0].hot_score.score > ranked[1].hot_score.score
    assert ranked[1].hot_score.score > ranked[2].hot_score.score

"""测试渲染输出是否包含指标和解释性组件"""
def test_demo_render_contains_metrics_and_explanatory_components() -> None:
    scenario = load_scenario()
    output = render_ranking(build_ranking(scenario), scenario)

    assert "模拟企业热点新闻排行榜" in output
    assert "AI数据中心正在面对下一道门槛" in output
    assert "news_id: 20260827A0C5VA00" in output
    assert "曝光=100" in output
    assert "分量:" in output
