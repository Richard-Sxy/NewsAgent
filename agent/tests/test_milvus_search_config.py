"""Shared YAML wiring, tenant filters, chat retrieval and no-fallback failures."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import sys

import pytest

from app.clients.enterprise.milvus_vector import MilvusTieredVectorIndex
from app.clients.knowledge_base import RelatedNewsSearchQuery
from app.conversation.tools import ConversationTools
from app.knowledge.search_config import load_knowledge_search_config
from app.knowledge.search_factory import build_knowledge_search_index, close_knowledge_search_index
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.knowledge_factory import build_native_knowledge_search
from app.model_runtime.local_embedding import LocalHashEmbedding
from app.retrieval.tiered_vector import VectorTier


def write_config(tmp_path, extra="", collections="    hot: news_hot_v1\n"):
    path = tmp_path / "search.yml"
    path.write_text(
        "schema_version: 1\nbackend: milvus\nmilvus:\n"
        "  uri: http://localhost:19530\n  database: default\n"
        "  collections:\n" + collections + extra,
        encoding="utf-8",
    )
    return str(path)


class MilvusClient:
    def __init__(self):
        self.calls = []
        self.close = Mock()

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return [[{"distance": 0.9, "entity": {
            "chunk_id": "chunk-1", "news_id": "news-1", "title": "新闻",
            "excerpt": "相关新闻正文", "embedding_version": "embedding-v1",
            "content_version": 1,
        }}]]


def test_default_and_explicit_postgres_do_not_connect(tmp_path, monkeypatch):
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(MilvusTieredVectorIndex, "connect", connect)
    store = object()
    assert build_knowledge_search_index(config_path=None, postgres_store=store) is store
    path = tmp_path / "postgres.yml"
    path.write_text("schema_version: 1\nbackend: postgres\n")
    assert build_knowledge_search_index(config_path=str(path), postgres_store=store) is store


def test_connect_uses_sdk_connection_and_search_settings(monkeypatch):
    client = MilvusClient()
    constructor = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "pymilvus", SimpleNamespace(MilvusClient=constructor))
    index = MilvusTieredVectorIndex.connect(uri="http://localhost:19530", token=None,
        database="news", collections={VectorTier.HOT: "news"}, timeout_seconds=3,
        metric_type="COSINE", search_params={})
    constructor.assert_called_once_with(uri="http://localhost:19530", token="", db_name="news", timeout=3)
    assert index.tiers == (VectorTier.HOT,)
    assert index._metric_type == "COSINE"
    assert index._search_params == {}


@pytest.mark.parametrize("extra", [
    "  token: secret\n", "  timeout_seconds: 0\n", "  metric_type: L2\n",
])
def test_invalid_config_rejected(tmp_path, extra):
    with pytest.raises(ValueError):
        load_knowledge_search_config(write_config(tmp_path, extra))


def test_duplicate_collections_rejected(tmp_path):
    with pytest.raises(ValueError, match="distinct"):
        load_knowledge_search_config(write_config(
            tmp_path, collections="    hot: news\n    warm: news\n"))


def test_missing_yaml_and_authenticated_uri_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_knowledge_search_config(tmp_path / "missing.yml")
    path = Path(write_config(tmp_path))
    path.write_text(path.read_text().replace("http://localhost", "http://user:pass@localhost"))
    with pytest.raises(ValueError, match="credentials"):
        load_knowledge_search_config(path)


def test_token_required_and_connection_settings_forwarded(tmp_path, monkeypatch):
    path = write_config(tmp_path, "  token_env: TEST_MILVUS_TOKEN\n  metric_type: COSINE\n  search_params: {}\n")
    connect = Mock(return_value=object())
    monkeypatch.setattr(MilvusTieredVectorIndex, "connect", connect)
    monkeypatch.delenv("TEST_MILVUS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="missing or empty"):
        build_knowledge_search_index(config_path=path, postgres_store=object())
    connect.assert_not_called()
    monkeypatch.setenv("TEST_MILVUS_TOKEN", "test-only-token")
    build_knowledge_search_index(config_path=path, postgres_store=object())
    assert connect.call_args.kwargs == {
        "uri": "http://localhost:19530", "token": "test-only-token", "database": "default",
        "collections": {VectorTier.HOT: "news_hot_v1"}, "timeout_seconds": 5,
        "metric_type": "COSINE", "search_params": {},
    }


@pytest.mark.asyncio
async def test_chat_search_reaches_configured_milvus_without_postgres(tmp_path, monkeypatch):
    client = MilvusClient()
    def connect(**kwargs):
        return MilvusTieredVectorIndex(client, collections=kwargs["collections"],
            metric_type=kwargs["metric_type"], search_params=kwargs["search_params"],
            timeout_seconds=kwargs["timeout_seconds"])
    monkeypatch.setattr(MilvusTieredVectorIndex, "connect", connect)
    postgres = SimpleNamespace(search=AsyncMock(side_effect=AssertionError("wrong backend")))
    index = build_knowledge_search_index(config_path=write_config(tmp_path), postgres_store=postgres)
    embedding = LocalHashEmbedding(model_routes=("embedding-v1",), dimensions=16)
    tools = ConversationTools(hot_news=None, knowledge_store=index, embedding=embedding,
                              embedding_version="embedding-v1", knowledge_enabled=True)
    result = await tools.execute(name="search_knowledge", arguments={"query": "新闻"},
                                 tenant_id="tenant-1", trace_id="turn-1")
    assert result["items"][0]["news_id"] == "news-1"
    assert result["evidence_is_untrusted"] is True
    assert len(client.calls) == 1  # Warm/cold are disabled, not duplicate searches.
    assert client.calls[0]["collection_name"] == "news_hot_v1"
    assert client.calls[0]["timeout"] == 5
    assert 'tenant_id == "tenant-1"' in client.calls[0]["filter"]
    assert 'embedding_version == "embedding-v1"' in client.calls[0]["filter"]
    postgres.search.assert_not_called()
    await close_knowledge_search_index(index)
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_hot_news_uses_same_index_and_configured_tiers():
    client = MilvusClient()
    index = MilvusTieredVectorIndex(client, collections={VectorTier.WARM: "news_warm_v1"})
    config = load_model_runtime_config(Path(__file__).parents[1] / "deploy/model-runtime.local.yml")
    embedding = LocalHashEmbedding(model_routes=("embedding-v1",), dimensions=16)
    # Use a matching embedding version for the fixture; no actual DB is accessed.
    config = config.model_copy(update={"embedding": config.embedding.model_copy(
        update={"model_routes": ("embedding-v1",)})})
    search = build_native_knowledge_search(config=config, embedding=embedding, database=None, index=index)
    result = await search.batch_search_related_news((RelatedNewsSearchQuery(
        query_id="query-1", source_news_id="source-1", title="新闻", summary="",
    ),), tenant_id="tenant-1")
    assert result["query-1"][0].news_id == "news-1"
    assert client.calls[0]["collection_name"] == "news_warm_v1"


@pytest.mark.asyncio
async def test_milvus_failure_does_not_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(MilvusTieredVectorIndex, "connect", Mock(side_effect=RuntimeError("offline")))
    postgres = SimpleNamespace(search=AsyncMock())
    with pytest.raises(RuntimeError, match="offline"):
        build_knowledge_search_index(config_path=write_config(tmp_path), postgres_store=postgres)
    client = MilvusClient()
    client.search = Mock(side_effect=RuntimeError("query offline"))
    index = MilvusTieredVectorIndex(client, collections={VectorTier.HOT: "news"})
    with pytest.raises(RuntimeError, match="query offline"):
        await index.search(tier=VectorTier.HOT, tenant_id="tenant-1", vector=(1.,),
            embedding_version="embedding-v1", exclude_news_ids=frozenset(), limit=3)
    postgres.search.assert_not_called()
