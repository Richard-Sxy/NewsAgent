今天完成的内容：
    hot_news_analysis.py
        这边完成了事实计算，然后做一个阈值分析，返回明确的指导建议，然后交给大模型去处理对象。
    热点分析持久化：
        hot_news_analysis
            news_id
            hot_score
            trend_summary
            attention_reasions
            related_contextx
            operation_suggestions
            evidence_news_ids
            created_at
        后续Agent和运营后台都直接消费这一个结果，不要每次重新计算
    项目构建思路：推送热点新闻 -> 获取热点新闻

    