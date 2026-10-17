"""Real cached content must remain bound to the same approved SQL/run identity."""

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.domain.errors import HotNewsDataQualityError
from app.modules.hot_news.application.hot_news_query import HotNewsQueryService
from app.sql_assistant.corpus_profiles import CORPUS_PROFILE, corpus_article, corpus_news_ids
from app.sql_assistant.hot_news_binding import HotNewsSqlBindingError, build_hot_news_sql_scope
from app.sql_assistant.planner import local_query_intent
from app.sql_assistant.scenarios import load_sql_scenarios
from app.sql_assistant.timeline_profiles import START, END
from app.sql_assistant.warehouse import (
    DEMO_TENANT_ID, SCHEMA_SHA256_V6, dataset_window, demo_dataset_info,
    get_text2sql_schema, verify_schema_contract, warehouse_contract,
)
from examples.data_analysis_demo import DemoError, _catalog
from simulation.native_hot_news_sql_support import SqlHotNewsContentRepository
from simulation import native_hot_news_sql_support as sql_support


TENANT = str(DEMO_TENANT_ID)


def test_corpus_sql_catalog_retains_real_content_and_explicit_synthetic_metrics():
    info = demo_dataset_info(CORPUS_PROFILE, news_per_tenant=120)
    assert info["schema_version"] == "news-warehouse-v6"
    assert info["schema_sha256"] == SCHEMA_SHA256_V6
    assert dataset_window(CORPUS_PROFILE) == (START, END)
    assert info["news_count"] == info["corpus_count"] == 120
    assert info["metric_row_count"] == info["baseline_row_count"] == 120 * 720
    assert info["news_content_source"] == "tencent-news-cache"
    assert info["metric_data_synthetic"] is info["category_data_synthetic"] is True
    assert info["corpus_catalog_sha256"]
    schema = get_text2sql_schema(CORPUS_PROFILE)
    assert "真实缓存新闻" in schema.tables[0].description
    assert "演示分类" in next(column.description for column in schema.tables[0].columns if column.name == "category")
    assert "原文缓存" in verify_schema_contract(CORPUS_PROFILE)
    config = {"dataset": info, "schema_version": info["schema_version"], "schema_sha256": info["schema_sha256"]}
    identity, scenarios = _catalog(config)
    assert identity["corpus_catalog_sha256"] == info["corpus_catalog_sha256"]
    assert len(scenarios) == 13
    changed = deepcopy(config)
    changed["dataset"]["metric_data_synthetic"] = False
    with pytest.raises(DemoError):
        _catalog(changed)
    changed = deepcopy(config)
    changed["dataset"]["news_content_source"] = "enterprise-live"
    with pytest.raises(DemoError):
        _catalog(changed)


def test_corpus_settings_and_scene_use_the_new_contract():
    settings = Settings(_env_file=None, environment="e2e",
                        database_url="postgresql+psycopg://test:test@127.0.0.1/test",
                        redis_url="redis://127.0.0.1:6379/0", temporal_address="127.0.0.1:7233",
                        temporal_namespace="default", artifact_bucket="test",
                        model_runtime_config_path="deploy/model-runtime.local.yml",
                        sql_assistant_dataset_profile=CORPUS_PROFILE, sql_assistant_news_per_tenant=120)
    assert settings.sql_assistant_dataset_profile == CORPUS_PROFILE
    scene = load_sql_scenarios("deploy/text2sql-scenes.corpus-v6.yml")
    assert scene.warehouse_schema_version == warehouse_contract(CORPUS_PROFILE).version
    assert scene.resolve("news-ranking").default_limit == 5
    scenario = scene.resolve("news-ranking")
    for question in scenario.sample_questions:
        intent = local_query_intent({"question": question, "scenario": scenario.model_dump(mode="json")})
        assert intent.row_limit == 5


def test_corpus_query_binding_requires_the_new_dataset_identity_and_fixed_size():
    info = demo_dataset_info(CORPUS_PROFILE, news_per_tenant=120)
    identity = {key: info[key] for key in ("dataset_profile", "dataset_version", "dataset_sha256", "news_per_tenant")}
    arguments = dict(question="查询点击量最高的前5条新闻", scenario_id="news-ranking",
                     window_start=START, window_end=START + timedelta(hours=1), schema_sha256=SCHEMA_SHA256_V6,
                     scenario={"sort": "clicks"}, production_bundle_version="bundle-v1",
                     model_scene="text2sql_assistant", warehouse_schema_version="news-warehouse-v6",
                     dataset_identity=identity)
    first = build_hot_news_sql_scope(**arguments)
    assert build_hot_news_sql_scope(**{**arguments, "dataset_identity": {**identity, "dataset_sha256": "0" * 64}}) != first
    with pytest.raises(HotNewsSqlBindingError):
        build_hot_news_sql_scope(**{**arguments, "dataset_identity": {**identity, "news_per_tenant": 1200}})
    with pytest.raises(HotNewsSqlBindingError):
        build_hot_news_sql_scope(**{**arguments, "warehouse_schema_version": "news-warehouse-v4"})


@pytest.mark.asyncio
async def test_corpus_body_title_time_and_original_url_reach_the_native_content_port():
    article = corpus_article(corpus_news_ids()[0])
    row = {"news_id": article["news_id"], "title": article["title"], "category": article["category"],
           "source": article["source"], "content_type": "article", "publish_time": datetime.fromisoformat(article["publish_time"])}

    async def fetch_metadata(*, news_ids):
        assert news_ids == (article["news_id"],)
        return [row]

    details = SimpleNamespace(tenant_id=TENANT, dataset_profile=CORPUS_PROFILE, fetch_metadata=fetch_metadata)
    result = await SqlHotNewsContentRepository(details).batch_get_by_news_ids(tenant_id=TENANT, news_ids=(article["news_id"],))
    content = result[article["news_id"]]
    assert content.body == article["content"]
    assert content.summary == article["content"][:300]
    assert content.title == article["title"]
    assert content.source_url == article["url"]
    assert content.publish_time.isoformat() == article["publish_time"]
    assert content.collection_id == "real-news-cache-v6"
    row["title"] = "another article"
    with pytest.raises(HotNewsDataQualityError, match="frozen content"):
        await SqlHotNewsContentRepository(details).batch_get_by_news_ids(tenant_id=TENANT, news_ids=(article["news_id"],))
    with pytest.raises(HotNewsDataQualityError, match="tenant"):
        await SqlHotNewsContentRepository(details).batch_get_by_news_ids(tenant_id="33333333-3333-4333-8333-333333333333", news_ids=(article["news_id"],))


def test_article_display_excerpts_are_bounded_and_cannot_cross_a_saved_news_identity():
    payload = {"analyzed_news": [
        {"news_id": "actual-1", "analysis_input": {"news_id": "actual-1", "content_excerpt": "缓存正文" * 300}},
        {"news_id": "actual-2", "analysis_input": {"news_id": "another-id", "content_excerpt": "foreign article"}},
        {"news_id": "old", "analysis_input": {"news_id": "old"}},
    ]}
    projection = HotNewsQueryService._index_news_content_excerpts(payload)
    assert projection == {"actual-1": ("缓存正文" * 300)[:600]}
    assert HotNewsQueryService._index_news_content_excerpts({"analyzed_news": None}) == {}


@pytest.mark.asyncio
async def test_real_news_cache_version_is_converted_at_the_knowledge_metadata_boundary(monkeypatch):
    article = corpus_article(corpus_news_ids()[0])

    async def fetch_metadata(*, news_ids):
        return [{**article, "publish_time": datetime.fromisoformat(article["publish_time"])}]

    details = SimpleNamespace(tenant_id=TENANT, dataset_profile=CORPUS_PROFILE, fetch_metadata=fetch_metadata)
    dependencies = SimpleNamespace(tenant_id=TENANT, dataset_profile=CORPUS_PROFILE,
                                   news_ids=(article["news_id"],), content_repository=SqlHotNewsContentRepository(details))
    captured = []

    async def capture_seed(dependencies, *, documents, store):
        for document in documents:
            document.validate()  # The durable knowledge Port requires flat string metadata.
            captured.append(document)

    monkeypatch.setattr(sql_support, "_seed_scaled_sql_knowledge", capture_seed)
    await sql_support.seed_native_sql_knowledge(dependencies, store=SimpleNamespace())
    assert len(captured) == 1
    document = captured[0]
    assert document.text == article["content"] and document.source_url == article["url"]
    assert document.metadata["content_version"] == str(article["content_version"])
    assert document.text_source == "real-news-cache-v6"
