"""The deployed reader-aware local assets preserve every historical Prompt."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from pydantic import TypeAdapter
import pytest
import yaml

from app.modules.data_loop.bundle_runtime import NativeProductionBundleRuntimeRegistry
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.factory import build_model_runtime_ports
from app.modules.hot_news.analysis.runner import HOT_NEWS_SCENE, local_hot_news_prompts
from app.modules.reader_profiles.contracts import PROFILE_POLICY_VERSION, ReaderInterestSummary, ReaderProfileSummary
from app.modules.hot_news.contracts.hot_news import HotNewsAnalysisInput, HotNewsMetrics, HotScoreComponents
from app.modules.data_loop.contracts.production_bundle import ProductionBundleSpec
from app.modules.hot_news.analysis.hot_news_analysis import HotNewsAnalysisValidator


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
READER_PROMPT = "native-e2e-prompt-v3-reader-profiles"


def test_reader_runtime_keeps_all_previous_prompt_keys_and_text_unchanged():
    previous = load_model_runtime_config(DEPLOY / "model-runtime.local.yml")
    reader = load_model_runtime_config(DEPLOY / "model-runtime.reader-profiles.local.yml")
    old_prompts = {(item.scene, item.version): item.system_prompt for item in previous.prompts}
    new_prompts = {(item.scene, item.version): item.system_prompt for item in reader.prompts}
    assert set(new_prompts) == set(old_prompts) | {(HOT_NEWS_SCENE, READER_PROMPT)}
    assert {key: new_prompts[key] for key in old_prompts} == old_prompts
    assert reader.inference == previous.inference
    assert reader.embedding == previous.embedding
    assert reader.agent_scenes == previous.agent_scenes


def test_deployed_reader_prompt_matches_python_versioned_asset_exactly():
    config = load_model_runtime_config(DEPLOY / "model-runtime.reader-profiles.local.yml")
    deployed = config.prompt_registry().resolve(scene=HOT_NEWS_SCENE, version=READER_PROMPT)
    python_asset = local_hot_news_prompts().resolve(scene=HOT_NEWS_SCENE, version=READER_PROMPT)
    assert deployed == python_asset
    assert "reader_profile_summary" in deployed.system_prompt
    assert "预期CTR" in deployed.system_prompt and "合成样例" in deployed.system_prompt


@pytest.mark.asyncio
async def test_actual_overlay_bundle_builds_and_runs_registered_local_reader_inference():
    overlay = yaml.safe_load((DEPLOY / "docker-compose.reader-profiles.local.yml").read_text())
    environment = overlay["x-reader-environment"]
    assert overlay["services"]["api"]["environment"] == environment
    assert overlay["services"]["hot-news-worker"]["environment"] == environment
    assert environment["MODEL_RUNTIME_CONFIG_PATH"] == "/app/deploy/model-runtime.reader-profiles.local.yml"
    assert environment["SQL_ASSISTANT_DATASET_PROFILE"] == "corpus-v6"
    assert environment["SQL_ASSISTANT_NEWS_PER_TENANT"] == "120"
    assert environment["SQL_ASSISTANT_SCENARIOS_PATH"] == "/app/deploy/text2sql-scenes.corpus-v6.yml"
    required_mounts = {"../agent/docs:/app/docs:ro", "../agent/data:/app/data:ro",
        "./text2sql-scenes.corpus-v6.yml:/app/deploy/text2sql-scenes.corpus-v6.yml:ro",
        "../var/reader-profiles/local-readers-v1-20261017:/app/local-reader-profiles:ro",
        "./model-runtime.reader-profiles.local.yml:/app/deploy/model-runtime.reader-profiles.local.yml:ro"}
    for name in ("api", "hot-news-worker"):
        assert required_mounts <= set(overlay["services"][name]["volumes"])
    specs = TypeAdapter(tuple[ProductionBundleSpec, ...]).validate_python(
        json.loads(environment["HOT_NEWS_RUNTIME_MANIFEST_JSON"]))
    assert specs and all(spec.analysis_prompt_version == READER_PROMPT for spec in specs)
    config = load_model_runtime_config(DEPLOY / "model-runtime.reader-profiles.local.yml")
    assert config.inference.provider == "local" and config.embedding.provider == "local"
    ports = build_model_runtime_ports(config, environment="e2e")
    try:
        registry = NativeProductionBundleRuntimeRegistry(ports.inference, ports.prompts, specs,
            allowed_model_routes=config.inference.model_routes)
        start = datetime(2026, 10, 14, 11, tzinfo=timezone.utc)
        profile = ReaderProfileSummary(news_id="reader-runtime-check", source_mode="synthetic",
            taxonomy_version="runtime-assets-topics-v1", profile_policy_version=PROFILE_POLICY_VERSION,
            reader_count=0, long_term=ReaderInterestSummary(lookback_hours=720, profiled_reader_count=0,
                unknown_reader_count=0, cold_start_reader_count=0, baseline_profiled_count=0),
            short_term=ReaderInterestSummary(lookback_hours=24, profiled_reader_count=0,
                unknown_reader_count=0, cold_start_reader_count=0, baseline_profiled_count=0),
            profile_cutoff=start, trigger_article_excluded=True, count_method="exact_synthetic_distinct", status="empty")
        for spec in specs:
            registry.ensure_supported(spec)
            runner = registry.create(spec, tenant_id="11111111-1111-4111-8111-111111111111")
            assert runner.prompt_version == READER_PROMPT
            assert runner.model_route in config.inference.model_routes
            analysis_input = HotNewsAnalysisInput(news_id="reader-runtime-check", title="读者画像配置功能检查",
                content_type="article", window_start=start, window_end=start + timedelta(hours=1), hot_score=.2,
                metrics=HotNewsMetrics(impressions=10, clicks=1, ctr=.1, unique_users=1,
                    effective_consumptions=0, interactions=0),
                score_components=HotScoreComponents(click=.1, consumption=.05, interaction=.05, growth=0),
                analysis_policy_version=spec.analysis_prompt_version, reader_profile_summary=profile)
            result = await runner.run(analysis_input)
            HotNewsAnalysisValidator().validate(analysis_input=analysis_input, result=result)
            assert result.value.news_id == analysis_input.news_id
            assert result.usage["local_stub"] is True
            assert any("合成" in limitation for limitation in result.value.limitations)
            assert any("没有可用" in limitation for limitation in result.value.limitations)
    finally:
        await ports.close()
