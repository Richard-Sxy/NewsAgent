"""BGE snapshot configuration and shared chat/hot-news retrieval contracts."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from app.analytics.entities import ContentType
from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.news_content import InMemoryNewsContentRepository, NewsContent
from app.clients.knowledge_base import RelatedNewsSearchQuery
from app.conversation.tools import ConversationToolDenied, ConversationTools
from app.knowledge.search_config import load_knowledge_search_config
from app.knowledge.search_factory import build_knowledge_search_index
from app.model_runtime.knowledge_factory import build_native_knowledge_search
from app.retrieval.tiered_vector import VectorSearchHit, VectorTier
from examples.hot_news_demo import build_ranking, load_scenario


TENANT = "tenant-1"
BGE_VERSION = "bge-test"


def write_snapshot_config(tmp_path: Path, **updates) -> Path:
    directory = tmp_path / "deploy"
    directory.mkdir(exist_ok=True)
    path = directory / "knowledge-search.yml"
    values = {
        "schema_version": 1,
        "backend": "bge_snapshot",
        "bge_snapshot": {
            "package_dir": "../rag-recall-demo",
            "tenant_id": TENANT,
            "snapshot_version": "snapshot-test-v1",
            "device": "cpu",
            "chunk_pool": 200,
            "ef": 256,
        },
    }
    values.update(updates)
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return path


def snapshot_hit(*, news_id="related-news", title="AI 数据中心相关新闻", excerpt="AI 数据中心新闻证据"):
    return VectorSearchHit(
        chunk_id=f"snapshot-test-v1:{news_id}:0",
        news_id=news_id,
        title=title,
        excerpt=excerpt,
        source_url=None,
        publish_time=None,
        raw_score=0.91,
        tier=VectorTier.HOT,
        content_version=1,
        embedding_version=BGE_VERSION,
    )


def fake_snapshot_index(*hits):
    return SimpleNamespace(
        query_embedder=SimpleNamespace(
            version=BGE_VERSION,
            embed_queries=AsyncMock(return_value=[(1.0, 0.0)]),
        ),
        tiers=(VectorTier.HOT,),
        search=AsyncMock(return_value=list(hits) or [snapshot_hit()]),
    )


def forbidden_embedding():
    return SimpleNamespace(embed=AsyncMock(side_effect=AssertionError("wrong embedding provider")))


def test_snapshot_yaml_resolves_package_relative_to_yaml_not_working_directory(tmp_path, monkeypatch):
    path = write_snapshot_config(tmp_path)
    unrelated = tmp_path / "unrelated-working-directory"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    config = load_knowledge_search_config(path)

    assert config.backend == "bge_snapshot"
    assert config.bge_snapshot.package_dir == str((tmp_path / "rag-recall-demo").resolve())
    assert config.bge_snapshot.tenant_id == TENANT
    assert config.bge_snapshot.snapshot_version == "snapshot-test-v1"


@pytest.mark.parametrize("updates", [
    {"backend": "postgres"},
    {"bge_snapshot": None},
    {"milvus": {"uri": "http://localhost:19530", "collections": {"hot": "news_hot"}}},
    {"bge_snapshot": {"package_dir": "../rag-recall-demo", "tenant_id": "", "snapshot_version": "v1"}},
    {"bge_snapshot": {"package_dir": "../rag-recall-demo", "tenant_id": "  ", "snapshot_version": "v1"}},
])
def test_snapshot_yaml_rejects_missing_identity_or_mixed_backends(tmp_path, updates):
    with pytest.raises(ValueError):
        load_knowledge_search_config(write_snapshot_config(tmp_path, **updates))


def test_factory_connects_configured_snapshot_without_touching_postgres(tmp_path, monkeypatch):
    from app.retrieval.bge_snapshot import BgeSnapshotVectorIndex

    path = write_snapshot_config(tmp_path)
    index = fake_snapshot_index()
    connect = Mock(return_value=index)
    monkeypatch.setattr(BgeSnapshotVectorIndex, "connect", connect)
    postgres = SimpleNamespace(search=AsyncMock(side_effect=AssertionError("wrong index")))

    result = build_knowledge_search_index(config_path=str(path), postgres_store=postgres)

    assert result is index
    connect.assert_called_once()
    passed_config = connect.call_args.args[0]
    assert passed_config.package_dir == str((tmp_path / "rag-recall-demo").resolve())
    assert passed_config.tenant_id == TENANT
    assert passed_config.snapshot_version == "snapshot-test-v1"
    postgres.search.assert_not_called()


def test_snapshot_load_failure_does_not_fall_back_to_postgres(tmp_path, monkeypatch):
    from app.retrieval.bge_snapshot import BgeSnapshotVectorIndex

    connect = Mock(side_effect=ValueError("snapshot integrity failure"))
    monkeypatch.setattr(BgeSnapshotVectorIndex, "connect", connect)
    postgres = SimpleNamespace(search=AsyncMock())

    with pytest.raises(ValueError, match="snapshot integrity failure"):
        build_knowledge_search_index(
            config_path=str(write_snapshot_config(tmp_path)), postgres_store=postgres,
        )

    connect.assert_called_once()
    postgres.search.assert_not_called()


@pytest.mark.asyncio
async def test_hot_news_uses_snapshot_query_encoder_and_excludes_source_article():
    index = fake_snapshot_index()
    embedding = forbidden_embedding()
    knowledge = build_native_knowledge_search(config=None, embedding=embedding, database=None, index=index)

    result = await knowledge.batch_search_related_news((RelatedNewsSearchQuery(
        query_id="query-1", source_news_id="source-news", title="AI 数据中心", summary="算力需求",
    ),), tenant_id=TENANT)

    index.query_embedder.embed_queries.assert_awaited_once_with(["AI 数据中心 算力需求"], tenant_id=TENANT)
    assert result["query-1"][0].news_id == "related-news"
    assert result["query-1"][0].publish_time is None
    assert result["query-1"][0].source_url is None
    index.search.assert_awaited_once()
    search_arguments = index.search.await_args.kwargs
    assert search_arguments["tenant_id"] == TENANT
    assert search_arguments["embedding_version"] == BGE_VERSION
    assert search_arguments["tier"] == VectorTier.HOT
    assert search_arguments["exclude_news_ids"] == frozenset({"source-news"})
    embedding.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_uses_same_snapshot_encoder_and_passes_source_exclusion():
    index = fake_snapshot_index()
    embedding = forbidden_embedding()
    tools = ConversationTools(
        hot_news=None, knowledge_store=index, embedding=embedding,
        embedding_version="unrelated-local-embedding", knowledge_enabled=True,
    )

    result = await tools.execute(
        name="search_knowledge", arguments={"query": "AI 数据中心", "source_news_id": "source-news"},
        tenant_id=TENANT, trace_id="chat-turn-1",
    )

    assert result["items"][0]["news_id"] == "related-news"
    assert result["items"][0]["embedding_version"] == BGE_VERSION
    assert result["items"][0]["publish_time"] is None
    assert result["items"][0]["source_url"] is None
    assert result["evidence_is_untrusted"] is True
    index.query_embedder.embed_queries.assert_awaited_once()
    assert list(index.query_embedder.embed_queries.await_args.args[0]) == ["AI 数据中心"]
    assert index.query_embedder.embed_queries.await_args.kwargs["tenant_id"] == TENANT
    index.search.assert_awaited_once()
    assert index.search.await_args.kwargs["embedding_version"] == BGE_VERSION
    assert index.search.await_args.kwargs["exclude_news_ids"] == frozenset({"source-news"})
    embedding.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_disabled_knowledge_cannot_load_or_query_snapshot():
    index = fake_snapshot_index()
    embedding = forbidden_embedding()
    tools = ConversationTools(
        hot_news=None, knowledge_store=index, embedding=embedding,
        embedding_version="unrelated-local-embedding", knowledge_enabled=False,
    )

    with pytest.raises(ConversationToolDenied, match="knowledge_read_not_enabled"):
        await tools.execute(name="search_knowledge", arguments={"query": "AI 数据中心"},
                            tenant_id=TENANT, trace_id="chat-turn-1")

    index.query_embedder.embed_queries.assert_not_awaited()
    index.search.assert_not_awaited()
    embedding.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_candidates_reach_enrichment_without_inventing_missing_metadata():
    scenario = load_scenario()
    ranked = build_ranking(scenario, limit=1)
    source_id = ranked[0].current.news_id
    row = next(item for item in scenario["news"] if item["news_id"] == source_id)
    content = NewsContent(
        news_id=source_id, title=row["title"], summary=row["summary"],
        content_type=ContentType(row["content_type"]),
        publish_time=datetime.fromisoformat(row["publish_time"]), source_url=row["source_url"],
    )
    # A buggy external index returning the source itself must not contaminate
    # the related-news evidence. Its valid candidate still has no metadata.
    index = fake_snapshot_index(
        snapshot_hit(news_id=source_id, title=content.title, excerpt=content.summary),
        snapshot_hit(news_id="historical-ai-news", title=content.title, excerpt=content.summary),
    )
    embedding = forbidden_embedding()
    knowledge = build_native_knowledge_search(config=None, embedding=embedding, database=None, index=index)
    service = HotNewsEnrichmentService(InMemoryNewsContentRepository([content]), knowledge)

    enriched = await service.enrich(ranked, tenant_id=TENANT, related_limit=2, candidate_limit=5)

    assert enriched[0].content == content
    assert [item.news.news_id for item in enriched[0].related_news] == ["historical-ai-news"]
    related = enriched[0].related_news[0]
    assert related.news.collection_id == "snapshot-test-v1:historical-ai-news:0"
    assert related.news.publish_time is None
    assert related.news.source_url is None
    assert related.time_score == 0.0
    assert related.final_score >= 0.3
    assert index.search.await_args.kwargs["exclude_news_ids"] == frozenset({source_id})
    embedding.embed.assert_not_awaited()
