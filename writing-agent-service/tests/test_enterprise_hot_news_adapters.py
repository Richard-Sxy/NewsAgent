from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.analytics.data_source import BehaviorQuery
from app.analytics.entities import ContentType, EventType
from app.clients.enterprise.baseline import (
    MetricBaselinePage,
    MetricBaselineRow,
    RpcHotNewsBaselineProvider,
)
from app.clients.enterprise.behavior_data import (
    BehaviorEventPage,
    BehaviorEventRow,
    BehaviorWatermarkResponse,
    RpcBehaviorDataSource,
)
from app.clients.enterprise.common import (
    EnterpriseRpcResponseError,
    RpcResponseMeta,
)
from app.clients.enterprise.news_content import (
    NewsContentBatchResponse,
    NewsContentRow,
    RpcNewsContentRepository,
)
from app.clients.enterprise.related_news import (
    RelatedNewsBatchResponse,
    RelatedNewsCandidate,
    RelatedNewsQueryResult,
    RpcRelatedNewsSearchClient,
)
from app.clients.knowledge_base import RelatedNewsSearchQuery
from app.domain.errors import HotNewsDataQualityError


START = datetime(2026, 9, 9, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def response_meta(request_id: str = "rpc-response-1") -> RpcResponseMeta:
    return RpcResponseMeta(
        request_id=request_id,
        source_system="enterprise-test-double",
        source_version="v1",
        served_at=END,
    )


class FakeBehaviorRpc:
    def __init__(self, *, complete: bool = True) -> None:
        self.complete = complete
        self.requests = []

    async def get_watermark(self, *, context, request):
        self.requests.append((context, request))
        return BehaviorWatermarkResponse(
            dataset=request.dataset,
            available_through=END if self.complete else START,
            is_complete=self.complete,
            meta=response_meta("watermark-1"),
        )

    async def query_events(self, *, context, request):
        self.requests.append((context, request))
        page_number = 1 if request.page_token is None else 2
        return BehaviorEventPage(
            rows=(
                BehaviorEventRow(
                    event_id=f"event-{page_number}",
                    anonymous_user_key="anonymous-user",
                    news_id="news-1",
                    event_type="READ_EVENT",
                    event_time=START + timedelta(minutes=page_number),
                    content_type="ARTICLE_CONTENT",
                    duration_milliseconds=2_500,
                ),
            ),
            next_page_token="page-2" if page_number == 1 else None,
            watermark=END,
            data_version="partition-v1",
            meta=response_meta(f"page-{page_number}"),
        )


@pytest.mark.asyncio
async def test_behavior_adapter_checks_watermark_and_maps_paginated_rows() -> None:
    rpc = FakeBehaviorRpc()
    adapter = RpcBehaviorDataSource(
        rpc,
        dataset="behavior-events-v1",
        event_type_mapping={"READ_EVENT": EventType.READ},
        content_type_mapping={"ARTICLE_CONTENT": ContentType.ARTICLE},
    )

    records = await adapter.fetch(
        BehaviorQuery(
            start=START,
            end=END,
            tenant_id="tenant-1",
            news_ids=frozenset({"news-1"}),
            content_types=frozenset({ContentType.ARTICLE}),
        )
    )

    assert [item.event_id for item in records] == ["event-1", "event-2"]
    assert all(item.duration_seconds == 2 for item in records)
    assert rpc.requests[1][1].page_token is None
    assert rpc.requests[2][1].page_token == "page-2"
    assert all(call[0].tenant_id == "tenant-1" for call in rpc.requests)


@pytest.mark.asyncio
async def test_behavior_adapter_rejects_incomplete_data_watermark() -> None:
    adapter = RpcBehaviorDataSource(
        FakeBehaviorRpc(complete=False),
        dataset="behavior-events-v1",
    )

    with pytest.raises(HotNewsDataQualityError, match="watermark"):
        await adapter.fetch(
            BehaviorQuery(
                start=START,
                end=END,
                tenant_id="tenant-1",
            )
        )


class FakeBaselineRpc:
    def __init__(self, *, policy_version: str = "baseline-v1") -> None:
        self.policy_version = policy_version

    async def query_baselines(self, *, context, request):
        key = request.metric_keys[0]
        return MetricBaselinePage(
            rows=(
                MetricBaselineRow(
                    news_id=key.news_id,
                    content_type=key.content_type,
                    sample_count=7,
                    impressions=Decimal("100"),
                    clicks=Decimal("20"),
                    unique_users=Decimal("80"),
                    total_duration_seconds=Decimal("900"),
                    effective_consumptions=Decimal("15"),
                    interactions=Decimal("5"),
                    ctr=Decimal("0.2"),
                ),
            ),
            baseline_policy_version=self.policy_version,
            data_version="baseline-partition-v1",
            meta=response_meta(),
        )


@pytest.mark.asyncio
async def test_baseline_adapter_maps_versioned_aggregate_snapshot() -> None:
    adapter = RpcHotNewsBaselineProvider(
        FakeBaselineRpc(),
        production_bundle_version="bundle-v1",
        baseline_policy_version="baseline-v1",
    )
    key = ("news-1", ContentType.ARTICLE)

    result = await adapter.get_baselines(
        tenant_id="tenant-1",
        window_start=START,
        window_end=END,
        production_bundle_version="bundle-v1",
        metric_keys=frozenset({key}),
    )

    assert result[key].sample_count == 7
    assert result[key].ctr == Decimal("0.2")


@pytest.mark.asyncio
async def test_baseline_adapter_rejects_policy_version_drift() -> None:
    adapter = RpcHotNewsBaselineProvider(
        FakeBaselineRpc(policy_version="baseline-v2"),
        production_bundle_version="bundle-v1",
        baseline_policy_version="baseline-v1",
    )

    with pytest.raises(HotNewsDataQualityError, match="policy version"):
        await adapter.get_baselines(
            tenant_id="tenant-1",
            window_start=START,
            window_end=END,
            production_bundle_version="bundle-v1",
            metric_keys=frozenset({("news-1", ContentType.ARTICLE)}),
        )


class FakeContentRpc:
    def __init__(self, *, omit_response: bool = False) -> None:
        self.omit_response = omit_response
        self.requests = []

    async def batch_get_news(self, *, context, request):
        self.requests.append(request)
        if self.omit_response:
            return NewsContentBatchResponse(items=(), meta=response_meta())

        items = tuple(
            NewsContentRow(
                news_id=news_id,
                title=f"新闻 {news_id}",
                summary="可信摘要",
                body="可信正文",
                content_type="article",
                publish_time=START,
                canonical_url=f"https://news.example.com/{news_id}",
                content_version="content-v1",
                status="published",
            )
            for news_id in request.news_ids
            if news_id != "missing"
        )
        return NewsContentBatchResponse(
            items=items,
            missing_news_ids=("missing",) if "missing" in request.news_ids else (),
            meta=response_meta(),
        )


@pytest.mark.asyncio
async def test_content_adapter_batches_and_preserves_explicit_missing_ids() -> None:
    rpc = FakeContentRpc()
    adapter = RpcNewsContentRepository(rpc, batch_size=1)

    result = await adapter.batch_get_by_news_ids(
        tenant_id="tenant-1",
        news_ids=("news-1", "missing"),
    )

    assert tuple(result) == ("news-1",)
    assert len(rpc.requests) == 2


@pytest.mark.asyncio
async def test_content_adapter_rejects_silent_partial_response() -> None:
    adapter = RpcNewsContentRepository(FakeContentRpc(omit_response=True))

    with pytest.raises(EnterpriseRpcResponseError, match="incomplete"):
        await adapter.batch_get_by_news_ids(
            tenant_id="tenant-1",
            news_ids=("news-1",),
        )


class FakeRetrievalRpc:
    async def batch_search_related_news(self, *, context, request):
        query = request.queries[0]
        return RelatedNewsBatchResponse(
            results=(
                RelatedNewsQueryResult(
                    query_id=query.query_id,
                    source_news_id=query.source_news_id,
                    candidates=(
                        RelatedNewsCandidate(
                            news_id=query.source_news_id,
                            document_id="self-document",
                            title="热点自身",
                            retrieval_channel="vector",
                            normalized_score=1,
                        ),
                        RelatedNewsCandidate(
                            news_id="news-2",
                            document_id="related-document",
                            title="相关报道",
                            excerpt="相关证据",
                            retrieval_channel="hybrid",
                            normalized_score=0.87,
                        ),
                        RelatedNewsCandidate(
                            news_id="news-2",
                            document_id="related-document",
                            title="重复切片",
                            retrieval_channel="keyword",
                            normalized_score=0.80,
                        ),
                    ),
                ),
            ),
            retrieval_policy_version=request.retrieval_policy_version,
            index_version="index-v1",
            meta=response_meta(),
        )


@pytest.mark.asyncio
async def test_retrieval_adapter_excludes_self_and_deduplicates_documents() -> None:
    adapter = RpcRelatedNewsSearchClient(
        FakeRetrievalRpc(),
        retrieval_policy_version="retrieval-v1",
    )
    query = RelatedNewsSearchQuery(
        query_id="query-1",
        source_news_id="news-1",
        title="热点新闻",
        summary="热点摘要",
    )

    result = await adapter.batch_search_related_news(
        (query,),
        tenant_id="tenant-1",
    )

    assert [item.news_id for item in result["query-1"]] == ["news-2"]
    assert result["query-1"][0].score == 0.87


def test_runtime_can_inject_enterprise_retrieval_without_fastgpt_dataset(
    monkeypatch,
) -> None:
    import app.hot_news_bootstrap as bootstrap

    model_client = SimpleNamespace(close=lambda: None)
    enterprise_search = RpcRelatedNewsSearchClient(
        FakeRetrievalRpc(),
        retrieval_policy_version="retrieval-v1",
    )
    monkeypatch.setattr(bootstrap, "FastGPTClient", lambda settings: model_client)

    runtime = bootstrap.create_hot_news_runtime(
        SimpleNamespace(
            fastgpt_hot_news_app_id="hot-news-agent",
            fastgpt_dataset_id=None,
        ),
        behavior_data_source=object(),
        baseline_provider=object(),
        content_repository=object(),
        policy=object(),
        knowledge_search=enterprise_search,
    )

    assert runtime.knowledge_client is enterprise_search
    assert runtime.service.enrichment_service.knowledge_search is enterprise_search
