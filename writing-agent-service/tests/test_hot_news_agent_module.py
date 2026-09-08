"""热点大模型主链路的单元和 HTTP 契约测试。"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest

from app.analytics.entities import ContentType
from app.analytics.hot_news_enrichment import EnrichedHotNews
from app.analytics.hot_score import HotScoreResult
from app.analytics.metrics import NewsMetricSnapshot
from app.analytics.news_content import NewsContent
from app.analytics.ranking import RankedHotNews
from app.clients.fastgpt import AgentResult, FastGPTClient
from app.clients.knowledge_base import RelatedNews
from app.domain.errors import AgentOutputValidationError
from app.schemas.hot_news import (
    AnalysisReason,
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
    HotNewsMetrics,
    HotScoreComponents,
    PromptMemoryContext,
    PromptMemoryItem,
    RelatedNewsContext,
    RelatedNewsEvidence,
)
from app.retrieval.related_news_reranker import RerankRelatedNews
from app.services.agents.hot_news import HotNewsAnalysisAgentRunner
from app.services.hot_news_analysis import (
    HotNewsAnalysisInputBuilder,
    HotNewsAnalysisService,
    HotNewsAnalysisValidator,
)


def enriched_hot_news() -> EnrichedHotNews:
    window_start = datetime(2026, 9, 4, 10, tzinfo=timezone.utc)
    window_end = datetime(2026, 9, 4, 11, tzinfo=timezone.utc)
    current = NewsMetricSnapshot(
        news_id="news-1",
        content_type=ContentType.ARTICLE,
        window_start=window_start,
        window_end=window_end,
        impressions=100,
        clicks=40,
        unique_users=60,
        total_duration_seconds=900,
        effective_consumptions=30,
        interactions=6,
        ctr=Decimal("0.4"),
    )
    ranking = RankedHotNews(
        rank=1,
        current=current,
        baseline=None,
        hot_score=HotScoreResult(
            news_id="news-1",
            score=Decimal("0.72"),
            click_component=Decimal("0.25"),
            consumption_component=Decimal("0.22"),
            interaction_component=Decimal("0.10"),
            growth_component=Decimal("0.15"),
        ),
    )
    content = NewsContent(
        news_id="news-1",
        title="测试热点新闻",
        summary="用于验证真实热点 Agent 模块的新闻摘要。",
        body="正文" * 1000,
        content_type=ContentType.ARTICLE,
        publish_time=datetime(2026, 9, 4, 9, tzinfo=timezone.utc),
        source_url="https://news.example.com/news-1",
    )
    related = RerankRelatedNews(
        news=RelatedNews(
            collection_id="collection-1",
            news_id="evidence-1",
            title="相关历史报道",
            text="这是允许模型引用的关联报道摘要。",
            source_url="https://news.example.com/evidence-1",
            publish_time="2026-09-03T08:00:00+00:00",
            score=0.91,
        ),
        final_score=0.86,
        vector_score=0.91,
        entity_score=0.80,
        event_score=0.70,
        keyword_score=0.60,
        time_score=0.80,
        reasons=("关键主体一致",),
    )
    return EnrichedHotNews(
        ranking=ranking,
        content=content,
        related_news=(related,),
    )


def analysis_input() -> HotNewsAnalysisInput:
    return HotNewsAnalysisInput(
        news_id="news-1",
        title="测试热点新闻",
        summary="用于验证真实热点 Agent 模块的新闻摘要。",
        content_type="article",
        window_start=datetime(2026, 9, 4, 10, tzinfo=timezone.utc),
        window_end=datetime(2026, 9, 4, 11, tzinfo=timezone.utc),
        hot_score=0.72,
        metrics=HotNewsMetrics(
            impressions=100,
            clicks=40,
            ctr=0.4,
            unique_users=60,
            effective_consumptions=30,
            interactions=6,
        ),
        score_components=HotScoreComponents(
            click=0.25,
            consumption=0.22,
            interaction=0.10,
            growth=0.15,
        ),
        related_news=[
            RelatedNewsEvidence(
                news_id="evidence-1",
                title="相关历史报道",
                excerpt="这是允许模型引用的关联报道摘要。",
                source_url="https://news.example.com/evidence-1",
                published_at=datetime(2026, 9, 3, 8, tzinfo=timezone.utc),
                final_score=0.86,
                rerank_reasons=["关键主体一致"],
            )
        ],
        analysis_policy_version="hot-news-analysis-v1",
    )


def prompt_memory_context(*, memory_id: UUID | None = None) -> PromptMemoryContext:
    selected_memory_id = memory_id or uuid4()
    return PromptMemoryContext(
        resolver_policy_version="memory-prompt-v1",
        resolved_at=datetime(2026, 9, 4, 10, tzinfo=timezone.utc),
        items=(
            PromptMemoryItem(
                memory_id=selected_memory_id,
                memory_key="output.language",
                memory_kind="temporary_preference",
                selected_tier="short_term",
                origin="explicit_user",
                summary="本次任务使用中文",
                value="zh-CN",
                confidence=1,
                version=1,
            ),
        ),
    )


def valid_report() -> HotNewsAnalysisReport:
    return HotNewsAnalysisReport(
        news_id="news-1",
        trend_assessment="当前新闻呈现较强关注趋势。",
        dominant_driver="click",
        attention_reasons=[
            AnalysisReason(
                reason_type="metric",
                statement="点击表现是主要热度驱动因素。",
                metric_keys=["click_component", "ctr"],
                evidence_news_ids=[],
                confidence=0.9,
                certainty="observed",
            )
        ],
        related_contexts=[],
        operation_suggestions=[],
        evidence_news_ids=[],
        limitations=[],
        overall_confidence=0.85,
    )


def test_input_builder_maps_enriched_hot_news_without_raw_user_data() -> None:
    item = enriched_hot_news()

    payload = HotNewsAnalysisInputBuilder().build(item)

    assert payload.news_id == item.ranking.current.news_id
    assert payload.title == item.content.title
    assert payload.window_start == item.ranking.current.window_start
    assert payload.metrics.unique_users == 60
    assert payload.score_components.click == 0.25
    assert len(payload.content_excerpt) == 2000
    assert len(payload.related_news) <= 5
    assert payload.related_news[0].news_id == "evidence-1"
    assert payload.analysis_policy_version == "hot-news-analysis-v1"
    assert "user_id" not in payload.model_dump_json()


def test_input_builder_includes_resolved_prompt_memory() -> None:
    memory_context = PromptMemoryContext(
        resolver_policy_version="memory-prompt-v1",
        resolved_at=datetime(2026, 9, 4, 10, tzinfo=timezone.utc),
    )

    payload = HotNewsAnalysisInputBuilder().build(
        enriched_hot_news(),
        memory_context=memory_context,
    )

    assert payload.memory_context is memory_context
    assert payload.model_dump(mode="json")["memory_context"] == (
        memory_context.model_dump(mode="json")
    )


@pytest.mark.asyncio
async def test_runner_uses_existing_structured_fastgpt_client() -> None:
    expected = AgentResult(valid_report(), "req-1", {"total_tokens": 100}, "{}")
    client = AsyncMock()
    client.run_structured.return_value = expected
    runner = HotNewsAnalysisAgentRunner(client, "hot-news-app")

    result = await runner.run(analysis_input())

    assert result is expected
    assert client.run_structured.await_args.kwargs == {
        "app_id": "hot-news-app",
        "payload": analysis_input(),
        "output_type": HotNewsAnalysisReport,
        "mode": "hot_news_analysis",
    }


def test_validator_rejects_unknown_evidence_news_id() -> None:
    report = valid_report().model_copy(
        update={"evidence_news_ids": ["invented-news-id"]}
    )
    result = AgentResult(report, "req-2", {}, report.model_dump_json())

    with pytest.raises(AgentOutputValidationError, match="evidence"):
        HotNewsAnalysisValidator().validate(
            analysis_input=analysis_input(),
            result=result,
        )


def test_validator_accepts_applied_memory_from_prompt_context() -> None:
    memory_id = uuid4()
    trusted_input = analysis_input().model_copy(
        update={"memory_context": prompt_memory_context(memory_id=memory_id)}
    )
    report = valid_report().model_copy(
        update={"applied_memory_ids": [memory_id]}
    )

    HotNewsAnalysisValidator().validate(
        analysis_input=trusted_input,
        result=AgentResult(report, "req-memory", {}, report.model_dump_json()),
    )


def test_validator_rejects_unknown_applied_memory_id() -> None:
    trusted_input = analysis_input().model_copy(
        update={"memory_context": prompt_memory_context()}
    )
    report = valid_report().model_copy(
        update={"applied_memory_ids": [uuid4()]}
    )

    with pytest.raises(AgentOutputValidationError, match="memory"):
        HotNewsAnalysisValidator().validate(
            analysis_input=trusted_input,
            result=AgentResult(
                report,
                "req-memory-unknown",
                {},
                report.model_dump_json(),
            ),
        )


def test_validator_rejects_memory_id_without_prompt_context() -> None:
    report = valid_report().model_copy(
        update={"applied_memory_ids": [uuid4()]}
    )

    with pytest.raises(AgentOutputValidationError, match="memory"):
        HotNewsAnalysisValidator().validate(
            analysis_input=analysis_input(),
            result=AgentResult(
                report,
                "req-memory-empty",
                {},
                report.model_dump_json(),
            ),
        )


def test_validator_rejects_duplicate_applied_memory_ids() -> None:
    memory_id = uuid4()
    trusted_input = analysis_input().model_copy(
        update={"memory_context": prompt_memory_context(memory_id=memory_id)}
    )
    report = valid_report().model_copy(
        update={"applied_memory_ids": [memory_id, memory_id]}
    )

    with pytest.raises(AgentOutputValidationError, match="duplicate"):
        HotNewsAnalysisValidator().validate(
            analysis_input=trusted_input,
            result=AgentResult(
                report,
                "req-memory-duplicate",
                {},
                report.model_dump_json(),
            ),
        )


def test_validator_requires_limitation_when_input_has_no_evidence() -> None:
    input_without_evidence = analysis_input().model_copy(update={"related_news": []})
    report = valid_report().model_copy(update={"limitations": []})
    result = AgentResult(report, "req-3", {}, report.model_dump_json())

    with pytest.raises(AgentOutputValidationError, match="limitation"):
        HotNewsAnalysisValidator().validate(
            analysis_input=input_without_evidence,
            result=result,
        )


def test_validator_rejects_mismatched_news_id_and_preserves_raw_result() -> None:
    report = valid_report().model_copy(update={"news_id": "other-news"})
    result = AgentResult(report, "req-news-id", {}, "raw model response")

    with pytest.raises(AgentOutputValidationError, match="news_id") as exc_info:
        HotNewsAnalysisValidator().validate(
            analysis_input=analysis_input(),
            result=result,
        )

    assert exc_info.value.request_id == "req-news-id"
    assert exc_info.value.raw_content == "raw model response"


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        (
            AnalysisReason(
                reason_type="metric",
                statement="声称由指标支持却没有引用指标。",
                confidence=0.5,
                certainty="observed",
            ),
            "metric_key",
        ),
        (
            AnalysisReason(
                reason_type="evidence",
                statement="声称由证据支持却没有引用证据。",
                confidence=0.5,
                certainty="observed",
            ),
            "evidence",
        ),
        (
            AnalysisReason(
                reason_type="hypothesis",
                statement="假设被错误标记为已观察事实。",
                confidence=0.5,
                certainty="observed",
            ),
            "inferred",
        ),
    ],
)
def test_validator_enforces_reason_semantics(
    reason: AnalysisReason,
    message: str,
) -> None:
    report = valid_report().model_copy(update={"attention_reasons": [reason]})

    with pytest.raises(AgentOutputValidationError, match=message):
        HotNewsAnalysisValidator().validate(
            analysis_input=analysis_input(),
            result=AgentResult(report, "req-reason", {}, report.model_dump_json()),
        )


def test_validator_rejects_nested_evidence_not_present_in_input() -> None:
    report = valid_report().model_copy(
        update={
            "related_contexts": [
                RelatedNewsContext(
                    statement="该背景引用了输入中不存在的证据。",
                    evidence_news_ids=["invented-news-id"],
                    confidence=0.5,
                )
            ]
        }
    )

    with pytest.raises(AgentOutputValidationError, match="evidence"):
        HotNewsAnalysisValidator().validate(
            analysis_input=analysis_input(),
            result=AgentResult(report, "req-nested", {}, report.model_dump_json()),
        )


def test_validator_rejects_related_context_when_input_has_no_evidence() -> None:
    input_without_evidence = analysis_input().model_copy(update={"related_news": []})
    report = valid_report().model_copy(
        update={
            "related_contexts": [
                RelatedNewsContext(
                    statement="无证据时不应输出关联背景。",
                    evidence_news_ids=["evidence-1"],
                    confidence=0.5,
                )
            ],
            "limitations": ["当前输入没有关联新闻证据。"],
        }
    )

    with pytest.raises(AgentOutputValidationError, match="related_contexts"):
        HotNewsAnalysisValidator().validate(
            analysis_input=input_without_evidence,
            result=AgentResult(report, "req-context", {}, report.model_dump_json()),
        )


@pytest.mark.asyncio
async def test_snapshot_preserves_nested_input_and_call_metadata() -> None:
    report = valid_report()
    expected = AgentResult(report, "req-snapshot", {"total_tokens": 12}, "raw")
    runner = AsyncMock()
    runner.run.return_value = expected
    service = HotNewsAnalysisService(
        input_builder=HotNewsAnalysisInputBuilder(),
        runner=runner,
        validator=HotNewsAnalysisValidator(),
    )

    execution = await service.analyze_with_snapshot(enriched_hot_news())

    runner.run.assert_awaited_once()
    sent_input = runner.run.await_args.args[0]
    assert execution.analysis_input == sent_input
    assert execution.analysis is expected
    assert execution.captured_at.tzinfo == timezone.utc
    assert execution.validated_at.tzinfo == timezone.utc
    assert execution.captured_at <= execution.validated_at
    sent_input.related_news[0].rerank_reasons.clear()
    sent_input.related_news.clear()
    assert execution.analysis_input.related_news[0].rerank_reasons


@pytest.mark.asyncio
async def test_snapshot_rejects_invalid_report() -> None:
    report = valid_report().model_copy(update={"news_id": "wrong-news"})
    runner = AsyncMock()
    runner.run.return_value = AgentResult(report, "req-invalid", {}, "raw")
    service = HotNewsAnalysisService(
        input_builder=HotNewsAnalysisInputBuilder(),
        runner=runner,
        validator=HotNewsAnalysisValidator(),
    )

    with pytest.raises(AgentOutputValidationError, match="news_id"):
        await service.analyze_with_snapshot(enriched_hot_news())
    runner.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_executes_complete_hot_news_agent_chain() -> None:
    memory_id = uuid4()
    expected_report = valid_report().model_copy(
        update={"applied_memory_ids": [memory_id]}
    )
    memory_context = PromptMemoryContext(
        resolver_policy_version="memory-prompt-v1",
        resolved_at=datetime(2026, 9, 4, 10, tzinfo=timezone.utc),
        items=(
            PromptMemoryItem(
                memory_id=memory_id,
                memory_key="output.language",
                memory_kind="temporary_preference",
                selected_tier="short_term",
                origin="explicit_user",
                summary="本次任务使用中文",
                value="zh-CN",
                confidence=1,
                version=1,
            ),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["appId"] == "hot-news-app"
        assert body["variables"]["mode"] == "hot_news_analysis"
        assert "properties" in body["variables"]["output_schema"]
        model_input = json.loads(body["messages"][0]["content"])
        assert model_input["news_id"] == "news-1"
        assert model_input["metrics"]["clicks"] == 40
        assert model_input["related_news"][0]["news_id"] == "evidence-1"
        assert model_input["memory_context"] == (
            memory_context.model_dump(mode="json")
        )
        assert model_input["memory_context"]["items"][0][
            "memory_id"
        ] == str(memory_id)
        assert "user_id" not in model_input
        return httpx.Response(
            200,
            headers={"x-request-id": "req-chain"},
            json={
                "choices": [
                    {
                        "message": {
                            "content": expected_report.model_dump_json(),
                        }
                    }
                ],
                "usage": {"total_tokens": 120},
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FastGPTClient(
        SimpleNamespace(
            fastgpt_base_url="https://fastgpt.example.com",
            fastgpt_api_key="test-secret",
        ),
        http_client=http_client,
    )
    service = HotNewsAnalysisService(
        input_builder=HotNewsAnalysisInputBuilder(),
        runner=HotNewsAnalysisAgentRunner(client, "hot-news-app"),
        validator=HotNewsAnalysisValidator(),
    )

    try:
        result = await service.analyze(
            enriched_hot_news(),
            memory_context=memory_context,
        )
    finally:
        await http_client.aclose()

    assert result.value == expected_report
    assert result.value.applied_memory_ids == [memory_id]
    assert result.request_id == "req-chain"
    assert result.usage == {"total_tokens": 120}
