"""把仓库内的离线场景装配为热点 Worker 依赖束（仅限本地联调）。

生产环境不要使用：行为数据、基线与正文全部来自
``examples/data/hot_news_scenario.json``，不具备任何企业数据语义。

注意：本工厂产出的行为源与基线提供方都**绑定场景固定窗口**
（``2026-09-03T10:00:00+08:00``，60 分钟），因此只能配合
``python -m examples.start_hot_news_workflow`` 这类「按场景窗口手工触发」的方式使用，
**不能**接入 ``HOT_NEWS_SCHEDULE_DEFINITIONS_JSON`` 的周期调度。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analytics.data_source import InMemoryBehaviorDataSource
from app.analytics.news_content import InMemoryNewsContentRepository
from app.clients.knowledge_base import KnowledgeSearchClient
from app.config import Settings
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
)
from examples.hot_news_e2e_knowledge import OfflineScenarioKnowledgeSearchClient
from examples.hot_news_e2e_support import build_hot_news_e2e_dependencies


@dataclass(frozen=True, slots=True)
class ScenarioHotNewsDependencies:
    """满足 HotNewsDependencyBundle 协议的本地场景依赖束。"""

    behavior_data_source: InMemoryBehaviorDataSource
    baseline_provider: HotNewsBaselineProvider
    content_repository: InMemoryNewsContentRepository
    policy: HotNewsOrchestrationPolicy
    knowledge_search: KnowledgeSearchClient


def build_from_scenario(settings: Settings) -> ScenarioHotNewsDependencies:
    """HOT_NEWS_DEPENDENCIES_FACTORY 的本地联调实现。

    ``settings`` 未使用，保留它是为了满足统一工厂签名。
    """

    _ = settings
    dependencies = build_hot_news_e2e_dependencies()
    return ScenarioHotNewsDependencies(
        behavior_data_source=dependencies.behavior_data_source,
        baseline_provider=dependencies.baseline_provider,
        content_repository=dependencies.content_repository,
        policy=dependencies.policy,
        knowledge_search=OfflineScenarioKnowledgeSearchClient(
            dependencies.content_repository,
            dependencies.news_ids,
        ),
    )