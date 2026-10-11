"""Exercise CLI routing and evidence semantics without loading BGE or a database."""

import json
from types import SimpleNamespace

import pytest

from app.clients.knowledge_base import RelatedNews
from tools import query_news_recall as cli


TENANT_ID = "snapshot-tenant"


class FakeIndex:
    snapshot_version = "snapshot-test-v1"
    embedding_version = "bge-test-v1"

    def __init__(self):
        self.closed = False
        self.article = {
            "title": "华为公布上半年经营业绩",
            "summary": "华为发布半年报。",
            "publish_time": "2026-08-31T12:00:00+08:00",
            "source_url": "https://example.invalid/source",
        }
        self.article_request = None

    def get_article(self, news_id, *, tenant_id):
        self.article_request = (news_id, tenant_id)
        return self.article


class FakeSearch:
    def __init__(self):
        self.queries = ()
        self.tenant_id = None
        self.error = None

    async def batch_search_related_news(self, queries, *, tenant_id):
        self.queries = queries
        self.tenant_id = tenant_id
        if self.error:
            raise self.error
        return {
            queries[0].query_id: [
                RelatedNews(
                    collection_id="snapshot-chunk-1", news_id="related-article",
                    title="华为上半年营收发布", text="华为发布上半年经营业绩。",
                    source_url=None, publish_time="2026-08-30T12:00:00+08:00",
                    score=0.98,
                ),
            ],
        }


@pytest.fixture
def runtime(monkeypatch):
    index = FakeIndex()
    search = FakeSearch()
    calls = []

    monkeypatch.setattr(cli, "load_knowledge_search_config", lambda _: SimpleNamespace(
        backend="bge_snapshot", bge_snapshot=SimpleNamespace(tenant_id=TENANT_ID),
    ))

    def build_index(**kwargs):
        calls.append(("index", kwargs))
        return index

    def build_search(**kwargs):
        calls.append(("search", kwargs))
        return search

    async def close_index(value):
        assert value is index
        index.closed = True

    monkeypatch.setattr(cli, "build_knowledge_search_index", build_index)
    monkeypatch.setattr(cli, "build_native_knowledge_search", build_search)
    monkeypatch.setattr(cli, "close_knowledge_search_index", close_index)
    return index, search, calls


def arguments(*extra):
    return cli.build_parser().parse_args(["--tenant-id", TENANT_ID, *extra])


@pytest.mark.asyncio
async def test_query_routes_to_native_search_without_database_and_excludes_source(runtime):
    index, search, calls = runtime
    output = await cli.query_news(arguments("--query", "华为半年报", "--source-news-id", "source", "--limit", "3"))

    assert calls == [
        ("index", {"config_path": str(cli.DEFAULT_CONFIG), "postgres_store": None}),
        ("search", {"config": None, "embedding": None, "database": None, "index": index}),
    ]
    assert search.tenant_id == TENANT_ID
    assert search.queries[0].source_news_id == "source"
    assert search.queries[0].exclude_news_ids == ("source",)
    assert search.queries[0].candidate_limit == 3
    assert output["mode"] == "semantic_recall"
    assert output["snapshot_version"] == "snapshot-test-v1"
    assert output["embedding_version"] == "bge-test-v1"
    assert output["evidence_is_untrusted"] is True
    assert output["items"][0]["retrieval_score"] == 0.98
    assert "hot_score" not in output["items"][0]
    assert "score" not in output["items"][0]
    assert index.closed


@pytest.mark.asyncio
async def test_question_has_reserved_source_marker_and_no_invented_news_identity(runtime):
    _, search, _ = runtime
    output = await cli.query_news(arguments("--query", "华为发布了哪些新消息？"))
    assert search.queries[0].source_news_id == cli.QUERY_SOURCE_ID
    assert search.queries[0].exclude_news_ids == ()
    assert output["source_news_id"] is None


@pytest.mark.asyncio
async def test_news_id_uses_snapshot_title_summary_and_actual_time_for_reranking(runtime):
    index, search, _ = runtime
    output = await cli.query_news(arguments("--news-id", "source", "--related", "--limit", "1"))
    assert index.article_request == ("source", TENANT_ID)
    assert search.queries[0].query_text == "华为公布上半年经营业绩 华为发布半年报。"
    assert search.queries[0].exclude_news_ids == ("source",)
    assert search.queries[0].candidate_limit == 50
    assert output["mode"] == "related_news_rerank"
    assert len(output["items"]) == 1
    assert output["items"][0]["related_score"] > 0
    assert "发布时间接近" in output["items"][0]["reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [(), ("--source-news-id", "source")])
async def test_related_without_reliable_source_metadata_stays_semantic(runtime, extra):
    _, search, _ = runtime
    output = await cli.query_news(arguments("--query", "华为半年报", "--related", *extra))
    assert output["mode"] == "semantic_recall"
    assert "rerank_note" in output
    assert "related_score" not in output["items"][0]
    assert search.queries[0].candidate_limit == 10


@pytest.mark.asyncio
async def test_explicit_source_metadata_enables_reranking(runtime):
    output = await cli.query_news(arguments(
        "--query", "华为公布上半年经营业绩", "--source-news-id", "source",
        "--publish-time", "2026-08-31T12:00:00+08:00", "--related",
    ))
    assert output["mode"] == "related_news_rerank"
    assert "related_score" in output["items"][0]


@pytest.mark.asyncio
async def test_foreign_tenant_is_rejected_before_loading_snapshot(runtime):
    _, _, calls = runtime
    args = arguments("--query", "新闻")
    args.tenant_id = "foreign-tenant"
    with pytest.raises(ValueError, match="tenant"):
        await cli.query_news(args)
    assert calls == []


@pytest.mark.asyncio
async def test_non_snapshot_config_is_rejected_without_loading_backend(runtime, monkeypatch):
    _, _, calls = runtime
    monkeypatch.setattr(cli, "load_knowledge_search_config", lambda _: SimpleNamespace(backend="milvus"))
    with pytest.raises(ValueError, match="bge_snapshot"):
        await cli.query_news(arguments("--query", "新闻"))
    assert calls == []


@pytest.mark.asyncio
async def test_missing_article_closes_snapshot_without_searching(runtime):
    index, search, _ = runtime
    index.article = None
    with pytest.raises(ValueError, match="does not exist"):
        await cli.query_news(arguments("--news-id", "missing"))
    assert index.closed
    assert search.queries == ()


@pytest.mark.asyncio
async def test_search_failure_closes_snapshot(runtime):
    index, search, _ = runtime
    search.error = RuntimeError("query encoder failed")
    with pytest.raises(RuntimeError, match="encoder"):
        await cli.query_news(arguments("--query", "新闻"))
    assert index.closed


@pytest.mark.parametrize("argv", [
    ["--query", "新闻"],
    ["--tenant-id", TENANT_ID],
    ["--tenant-id", " ", "--query", "新闻"],
    ["--tenant-id", TENANT_ID, "--query", " "],
    ["--tenant-id", TENANT_ID, "--query", "新闻", "--limit", "0"],
    ["--tenant-id", TENANT_ID, "--query", "新闻", "--limit", "51"],
    ["--tenant-id", TENANT_ID, "--query", "新闻", "--news-id", "source"],
    ["--tenant-id", TENANT_ID, "--query", "新闻", "--publish-time", "2026-08-31T12:00:00"],
])
def test_invalid_cli_input_is_rejected(argv):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2


def test_main_prints_machine_readable_json(runtime, capsys):
    assert cli.main(["--tenant-id", TENANT_ID, "--query", "华为半年报"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["query"] == "华为半年报"
    assert output["items"][0]["news_id"] == "related-article"
