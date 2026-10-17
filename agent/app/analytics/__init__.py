"""用户行为指标与热点分析领域模块。

本模块只消费企业数据库返回的数据，不负责埋点、日志采集或数仓建设。
热点打分与富化实现已迁移到 app.modules.hot_news.scoring/analysis；
本包保留共享词汇表（entities/news_content/sql_guard）的急切导出，
迁移后的名称通过 __getattr__ 惰性再导出，避免与迁移模块形成导入环。
"""

from importlib import import_module as _import_module

from app.analytics.entities import BehaviorRecord, ContentType, EventType
from app.analytics.sql_guard import SqlGuard, SqlGuardPolicy
from app.analytics.news_content import (
    InMemoryNewsContentRepository,
    NewsContent,
    NewsContentRepository,
    TencentNewsCacheRepository,
)

_MOVED_REEXPORTS = {
    "BehaviorDataSource": "app.modules.hot_news.scoring.data_source",
    "BehaviorQuery": "app.modules.hot_news.scoring.data_source",
    "BaselineCalculator": "app.modules.hot_news.scoring.baseline",
    "NewsMetricBaseline": "app.modules.hot_news.scoring.baseline",
    "HotScoreCalculator": "app.modules.hot_news.scoring.hot_score",
    "HotScoreConfig": "app.modules.hot_news.scoring.hot_score",
    "HotScoreResult": "app.modules.hot_news.scoring.hot_score",
    "EnrichedHotNews": "app.modules.hot_news.analysis.hot_news_enrichment",
    "HotNewsEnrichmentService": "app.modules.hot_news.analysis.hot_news_enrichment",
    "NewsMetricCalculator": "app.modules.hot_news.scoring.metrics",
    "NewsMetricSnapshot": "app.modules.hot_news.scoring.metrics",
    "HotNewsMetricQuery": "app.modules.hot_news.scoring.metric_source",
    "NewsMetricSource": "app.modules.hot_news.scoring.metric_source",
    "TencentIngestContentRepository": "app.modules.hot_news.analysis.tencent_content_bridge",
    "CorpusKnowledgeSearchClient": "app.modules.hot_news.analysis.corpus_knowledge_search",
    "SearchableNewsContentRepository": "app.modules.hot_news.analysis.corpus_knowledge_search",
    "HotNewsRanker": "app.modules.hot_news.scoring.ranking",
    "RankedHotNews": "app.modules.hot_news.scoring.ranking",
}


def __getattr__(name: str):
    module_name = _MOVED_REEXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_import_module(module_name), name)


__all__ = [
    "BehaviorDataSource",
    "BehaviorQuery",
    "BehaviorRecord",
    "BaselineCalculator",
    "ContentType",
    "EventType",
    "EnrichedHotNews",
    "HotScoreCalculator",
    "HotScoreConfig",
    "HotScoreResult",
    "HotNewsRanker",
    "HotNewsEnrichmentService",
    "InMemoryNewsContentRepository",
    "NewsContent",
    "NewsContentRepository",
    "TencentNewsCacheRepository",
    "TencentIngestContentRepository",
    "CorpusKnowledgeSearchClient",
    "SearchableNewsContentRepository",
    "NewsMetricBaseline",
    "NewsMetricCalculator",
    "NewsMetricSnapshot",
    "HotNewsMetricQuery",
    "NewsMetricSource",
    "RankedHotNews",
    "SqlGuard",
    "SqlGuardPolicy",
]
