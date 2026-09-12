"""用户行为指标与热点分析领域模块。

本模块只消费企业数据库返回的数据，不负责埋点、日志采集或数仓建设。
"""

from app.analytics.data_source import BehaviorDataSource, BehaviorQuery
from app.analytics.baseline import BaselineCalculator, NewsMetricBaseline
from app.analytics.entities import BehaviorRecord, ContentType, EventType
from app.analytics.hot_score import HotScoreCalculator, HotScoreConfig, HotScoreResult
from app.analytics.hot_news_enrichment import EnrichedHotNews, HotNewsEnrichmentService
from app.analytics.metrics import NewsMetricCalculator, NewsMetricSnapshot
from app.analytics.metric_source import HotNewsMetricQuery, NewsMetricSource
from app.analytics.sql_guard import SqlGuard, SqlGuardPolicy
from app.analytics.news_content import (
    InMemoryNewsContentRepository,
    NewsContent,
    NewsContentRepository,
    TencentNewsCacheRepository,
)
from app.analytics.ranking import HotNewsRanker, RankedHotNews

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
    "NewsMetricBaseline",
    "NewsMetricCalculator",
    "NewsMetricSnapshot",
    "HotNewsMetricQuery",
    "NewsMetricSource",
    "RankedHotNews",
    "SqlGuard",
    "SqlGuardPolicy",
]
