"""热点分析所需企业 RPC Client 的依赖注入集合。"""

from dataclasses import dataclass
from typing import Mapping

from app.analytics.entities import ContentType, EventType
from app.clients.enterprise.baseline import (
    MetricBaselineWarehouseRpc,
    RpcHotNewsBaselineProvider,
)
from app.clients.enterprise.behavior_data import (
    BehaviorWarehouseRpc,
    RpcBehaviorDataSource,
)
from app.clients.enterprise.common import (
    DefaultRpcCallContextProvider,
    RpcCallContextProvider,
)
from app.clients.enterprise.model_gateway import StructuredModelGatewayRpc
from app.clients.enterprise.news_content import (
    NewsContentRpc,
    RpcNewsContentRepository,
)
from app.clients.enterprise.related_news import (
    RelatedNewsRetrievalRpc,
    RpcRelatedNewsSearchClient,
)


@dataclass(frozen=True, slots=True)
class EnterpriseHotNewsRpcClients:
    """Bootstrap 只依赖该集合，不直接依赖任何企业生成 SDK。"""

    behavior: BehaviorWarehouseRpc
    baseline: MetricBaselineWarehouseRpc
    content: NewsContentRpc
    related_news: RelatedNewsRetrievalRpc
    model_gateway: StructuredModelGatewayRpc | None = None


@dataclass(frozen=True, slots=True)
class EnterpriseHotNewsAdapterConfig:
    """可纳入 Production Bundle 的企业数据接入配置。"""

    behavior_dataset: str
    production_bundle_version: str
    baseline_policy_version: str
    retrieval_policy_version: str
    behavior_timeout_ms: int = 30_000
    baseline_timeout_ms: int = 15_000
    content_timeout_ms: int = 15_000
    retrieval_timeout_ms: int = 20_000


@dataclass(frozen=True, slots=True)
class EnterpriseHotNewsAdapters:
    """可直接传给热点编排层的领域 Adapter。"""

    behavior_data_source: RpcBehaviorDataSource
    baseline_provider: RpcHotNewsBaselineProvider
    content_repository: RpcNewsContentRepository
    related_news_search: RpcRelatedNewsSearchClient


def build_enterprise_hot_news_adapters(
    clients: EnterpriseHotNewsRpcClients,
    *,
    config: EnterpriseHotNewsAdapterConfig,
    context_provider: RpcCallContextProvider | None = None,
    behavior_event_type_mapping: Mapping[str, EventType] | None = None,
    behavior_content_type_mapping: Mapping[str, ContentType] | None = None,
) -> EnterpriseHotNewsAdapters:
    """集中装配企业 RPC 防腐层，避免 Service 内部创建客户端。"""

    provider = context_provider or DefaultRpcCallContextProvider()
    return EnterpriseHotNewsAdapters(
        behavior_data_source=RpcBehaviorDataSource(
            clients.behavior,
            dataset=config.behavior_dataset,
            context_provider=provider,
            event_type_mapping=(
                dict(behavior_event_type_mapping)
                if behavior_event_type_mapping is not None
                else None
            ),
            content_type_mapping=(
                dict(behavior_content_type_mapping)
                if behavior_content_type_mapping is not None
                else None
            ),
            timeout_ms=config.behavior_timeout_ms,
        ),
        baseline_provider=RpcHotNewsBaselineProvider(
            clients.baseline,
            production_bundle_version=config.production_bundle_version,
            baseline_policy_version=config.baseline_policy_version,
            context_provider=provider,
            timeout_ms=config.baseline_timeout_ms,
        ),
        content_repository=RpcNewsContentRepository(
            clients.content,
            context_provider=provider,
            timeout_ms=config.content_timeout_ms,
        ),
        related_news_search=RpcRelatedNewsSearchClient(
            clients.related_news,
            retrieval_policy_version=config.retrieval_policy_version,
            context_provider=provider,
            timeout_ms=config.retrieval_timeout_ms,
        ),
    )
