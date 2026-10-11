"""Opt-in acceptance of the shipped local model/corpus and application ports."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.conversation.tools import ConversationTools
from app.conversation.streaming import StreamingConversation
from app.knowledge.search_factory import build_knowledge_search_index, close_knowledge_search_index
from app.model_runtime.knowledge_factory import build_native_knowledge_search
from app.clients.knowledge_base import RelatedNewsSearchQuery
from app.services.hot_news_analysis import HotNewsAnalysisInputBuilder
from tests.test_hot_news_enrichment import content_repository_from_scenario
from tests.test_conversation_agent import RUN, USER, runtime
from tests.test_conversation_article_delivery import HotNews, decode_events
from examples.hot_news_demo import build_ranking, load_scenario


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("RUN_BGE_SNAPSHOT_TESTS") != "1",
    reason="explicit opt-in required for loading the 1.3 GB local BGE model",
)]
TENANT = "11111111-1111-4111-8111-111111111111"
SOURCE_ID = "20260719A04P4Y00"
CONFIG = Path(__file__).resolve().parents[1] / "deploy/knowledge-search.bge-snapshot.yml"


@pytest.fixture(scope="module")
def index():
    value = build_knowledge_search_index(config_path=str(CONFIG), postgres_store=None)
    yield value
    asyncio.run(close_knowledge_search_index(value))


@pytest.mark.asyncio
async def test_real_related_recall_and_chat_share_the_frozen_bge_model(index):
    source = index.get_article(SOURCE_ID, tenant_id=TENANT)
    search = build_native_knowledge_search(config=None, embedding=None, database=None, index=index)
    result = await search.batch_search_related_news((RelatedNewsSearchQuery(
        query_id="source", source_news_id=SOURCE_ID, title=source["title"],
        summary="", candidate_limit=5,
    ),), tenant_id=TENANT)
    candidates = result["source"]
    assert len(candidates) == 5
    assert SOURCE_ID not in {item.news_id for item in candidates}
    assert len({item.news_id for item in candidates}) == 5
    assert all(item.collection_id.startswith(f"bge-snapshot:{index.manifest_digest}:") for item in candidates)
    assert all(item.source_url is None and item.publish_time is None for item in candidates)

    tools = ConversationTools(hot_news=None, knowledge_store=index, embedding=None,
                              embedding_version="unrelated-model", knowledge_enabled=True)
    response = await tools.execute(name="search_knowledge", arguments={
        "query": source["title"], "source_news_id": f" {SOURCE_ID} ", "limit": 5,
    }, tenant_id=TENANT, trace_id="local-bge-acceptance")
    assert [item["news_id"] for item in response["items"]] == [item.news_id for item in candidates]
    assert response["snapshot_version"] == index.snapshot_version
    assert response["embedding_version"] == index.embedding_version
    assert response["evidence_is_untrusted"] is True


@pytest.mark.asyncio
async def test_real_snapshot_evidence_enters_existing_hot_news_analysis_input(index):
    # Rankings and source content here are the existing explicit synthetic
    # scenario; only recall uses real snapshot/model files. No behavior store,
    # Workflow, model inference, production bundle or publication is modified.
    ranked = build_ranking(load_scenario(), limit=1)
    search = build_native_knowledge_search(config=None, embedding=None, database=None, index=index)
    enriched = await HotNewsEnrichmentService(content_repository_from_scenario(), search).enrich(
        ranked, tenant_id=TENANT, candidate_limit=20, related_limit=3,
    )
    assert enriched[0].related_news
    analysis_input = HotNewsAnalysisInputBuilder().build(enriched[0])
    assert analysis_input.news_id == ranked[0].current.news_id
    assert analysis_input.hot_score == float(ranked[0].hot_score.score)
    assert analysis_input.related_news
    assert all(item.news_id != analysis_input.news_id for item in analysis_input.related_news)
    assert all(item.published_at is None for item in analysis_input.related_news)


@pytest.mark.asyncio
async def test_real_bge_articles_are_automatically_saved_and_delivered_in_hot_conversation(index, monkeypatch):
    # Only the source title and BGE recall are real snapshot data. The report's
    # metrics/ranking are explicit synthetic fixtures and planning uses the
    # existing local model Port; this does not validate enterprise model quality.
    source = index.get_article(SOURCE_ID, tenant_id=TENANT)
    assert source is not None and source["title"]
    hot_news = HotNews(count=1)
    hot_news.items[0].news_id = SOURCE_ID
    hot_news.items[0].title = source["title"]
    hot_news.items[0].metrics = hot_news.items[0].metrics.model_copy(update={"news_id": SOURCE_ID})
    metrics = hot_news.items[0].metrics.model_dump(mode="json")
    tools = ConversationTools(hot_news=hot_news, knowledge_store=index, embedding=None,
                              embedding_version="unrelated-model", knowledge_enabled=True)
    service, repository, inference, _ = runtime(local_simulation=True)
    service._tools = tools
    conversation = await repository.create(TENANT, USER, "真实快照关联召回验收")
    request = dict(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
                   request_id=uuid4(), content=f"读取热点运行 {RUN}")
    original_embed = index.query_embedder.embed_queries
    embed_calls = []

    async def counted_embed(texts, *, tenant_id):
        embed_calls.append((tuple(texts), tenant_id))
        return await original_embed(texts, tenant_id=tenant_id)

    async def events_for(claim):
        stream = StreamingConversation(
            service=service, claim=claim, tenant_id=TENANT, user_id=USER,
            conversation_id=conversation.id, heartbeat_seconds=0.03,
        )
        return decode_events("".join([frame async for frame in stream.iter_events()]))

    # Restore the module-scoped encoder before any later test or fixture close.
    with monkeypatch.context() as patch:
        patch.setattr(index.query_embedder, "embed_queries", counted_embed)
        events = await events_for(await service.prepare_turn(**request))
        assert events[-1][0] == "done"
        done = events[-1][1]["turn"]
        assert done["status"] == "completed" and len(done["tools"]) == 1
        trace = done["tools"][0]
        assert trace["name"] == "read_hot_news" and trace["status"] == "completed"
        report = trace["result"]
        assert report["items"][0]["metrics"] == metrics
        assert report["items"][0]["hot_score"]["score"] == "0.8000"
        group, = report["related_articles"]
        assert group["status"] == "completed"
        assert group["source_run_id"] == report["run_id"] == RUN
        assert group["source_news_id"] == SOURCE_ID and group["source_title"] == source["title"][:200]
        assert group["evidence_is_untrusted"] is True
        assert group["retrieval_backend"] == "bge_snapshot" and group["snapshot_version"] == index.snapshot_version
        cards = group["items"]
        assert len(cards) == len({card["news_id"] for card in cards}) == 3
        assert SOURCE_ID not in {card["news_id"] for card in cards}
        assert all(card["chunk_id"].startswith(f"bge-snapshot:{index.manifest_digest}:")
                   and card["embedding_version"] == index.embedding_version
                   and 0 < len(card["excerpt"]) <= 600
                   and card["source_url"] is None and card["publish_time"] is None
                   for card in cards)
        assert embed_calls == [((source["title"][:200],), TENANT)]
        assert len(inference.calls) == 2
        final_input = json.loads(inference.calls[-1].messages[1].content)
        assert "related_articles" not in final_input["tool_results"][0]["result"]
        assert all(card["excerpt"] not in inference.calls[-1].messages[1].content for card in cards)
        assert final_input["tool_results"][0]["result"]["items"][0]["metrics"] == metrics
        assert repository.checkpoint_calls[-1].tools[0].result["related_articles"] == [group]
        history = await repository.get(TENANT, USER, conversation.id)
        assert history.turns[0].model_dump(mode="json") == done
        replay = await events_for(await service.prepare_turn(**request))
        assert replay[0][1]["replayed"] is True and replay[-1] == ("done", {"turn": done})
        assert len(embed_calls) == 1 and len(inference.calls) == 2
