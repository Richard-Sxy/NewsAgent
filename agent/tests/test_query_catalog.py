"""Development context contracts; no live model or warehouse is contacted."""
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlglot

from app.modules.conversation import query_catalog as catalog
from app.modules.conversation.query_understanding import QueryPolicy, QueryUnderstanding
from app.modules.conversation.structured_query_resolution import resolve_structured_query
from app.schemas.sql_assistant import SqlAssistantIntent
from app.sql_assistant.planner import compile_query
from app.sql_assistant.scenarios import SqlScenario
from evaluation.query_semantics_runner import _payload
from simulation.conversation_hot_news import LocalConversationHotNewsQuery


NOW = datetime.fromisoformat("2026-10-03T12:30:00+08:00")


@pytest.fixture
def policy():
    return QueryPolicy(
        scenario=SqlScenario(
            id="news-ranking", name="测试", description="合同测试",
            default_sort="ctr", allowed_sort_metrics=("ctr", "clicks"),
            allowed_categories=("科技",), allowed_content_types=("article",),
            default_limit=5, max_limit=20, sample_questions=("科技图文点击率前5条",),
        ),
        default_limit=5, max_limit=10,
        supported_window_start="2026-10-03T10:00:00+08:00",
        supported_window_end="2026-10-03T11:00:00+08:00",
        coverage_window_start="2026-10-03T00:00:00+08:00",
        coverage_window_end="2026-10-04T00:00:00+08:00",
        data_watermark="2026-10-03T12:00:00+08:00",
    )


def test_context_is_scoped_draft_material_and_does_not_mutate_policy(policy):
    original = policy.model_dump()
    context = catalog.build_query_context(policy)
    assert context["version"] == "news-query-context-v1"
    assert context["production_approved"] is False
    assert context["review_status"] == "development_draft_not_human_reviewed"
    assert set(context["metrics"]) == {"ctr", "clicks"}
    assert context["capabilities"]["allowed_categories"] == ["科技"]
    assert context["capabilities"]["allowed_content_types"] == ["article"]
    assert context["capabilities"]["max_limit"] == 10
    assert {e["proposal"]["status"] for e in context["examples"]} == {"ready", "clarify", "unsupported"}
    assert all(e["split"] == "development" for e in context["examples"])
    assert all(e["review_status"] == context["review_status"] for e in context["examples"])
    ready = context["examples"][0]
    resolved = resolve_structured_query(ready["question"], ready["proposal"], policy, now=NOW).require_ready()
    assert resolved.intent.category == "科技"
    assert resolved.intent.content_type == "article"
    assert resolved.intent.row_limit == policy.default_limit
    assert policy.model_dump() == original


def test_context_formulas_match_current_compiler(policy):
    all_metrics = ("clicks", "impressions", "interactions", "ctr", "hot_score")
    scenario = policy.scenario.model_copy(update={"allowed_sort_metrics": all_metrics})
    context = catalog.build_query_context(policy.model_copy(update={"scenario": scenario}))
    sql = compile_query(SqlAssistantIntent(sort_by="ctr", row_limit=5, explanation="test"), scenario)
    projections = {item.alias: item.this for item in sqlglot.parse_one(sql, read="postgres").expressions
                   if isinstance(item, sqlglot.exp.Alias)}
    for metric, definition in context["metrics"].items():
        expected = sqlglot.parse_one(definition["formula"], read="postgres")
        assert projections[metric] == expected
    assert "零曝光返回0" in context["metrics"]["ctr"]["meaning"]
    assert "Python" in context["metrics"]["hot_score"]["meaning"]


def test_context_identity_changes_with_scope_without_cross_scope_examples(policy):
    first = catalog.build_query_context(policy)
    scenario = policy.scenario.model_copy(update={
        "allowed_categories": ("财经",), "allowed_content_types": ("video",),
        "allowed_sort_metrics": ("clicks",), "default_sort": "clicks",
    })
    second = catalog.build_query_context(policy.model_copy(update={"scenario": scenario}))
    assert first["asset_sha256"] == second["asset_sha256"] == catalog.CATALOG_SHA256
    assert first["context_sha256"] != second["context_sha256"]
    assert first["policy_sha256"] != second["policy_sha256"]
    assert set(second["metrics"]) == {"clicks"}
    assert "科技" not in json.dumps(second, ensure_ascii=False)
    assert second["examples"][0]["proposal"]["intent"]["content_type"] == "video"


def test_unimplemented_ranking_source_does_not_gain_a_ready_example(policy):
    context = catalog.build_query_context(policy.model_copy(update={"allowed_ranking_sources": ("enterprise_board",)}))
    assert context["capabilities"]["allowed_ranking_sources"] == []
    assert all(e["proposal"]["status"] != "ready" for e in context["examples"])


@pytest.mark.parametrize("budget", [0, -1, True, 12001])
def test_invalid_context_budget_is_rejected(policy, budget):
    with pytest.raises(ValueError, match="budget"):
        catalog.build_query_context(policy, max_chars=budget)


def test_oversized_context_fails_without_truncating_rules(policy):
    with pytest.raises(ValueError, match="exceeds"):
        catalog.build_query_context(policy, max_chars=100)


def test_modified_asset_is_rejected(policy, tmp_path, monkeypatch):
    asset = tmp_path / "catalog.json"
    asset.write_bytes(catalog.CATALOG_PATH.read_bytes() + b" ")
    monkeypatch.setattr(catalog, "CATALOG_PATH", asset)
    with pytest.raises(ValueError, match="identity mismatch"):
        catalog.build_query_context(policy)


def test_default_payload_never_loads_optional_asset(policy, monkeypatch):
    monkeypatch.setattr(catalog, "_load_catalog", lambda: pytest.fail("disabled catalog must not load"))
    payload = catalog.build_query_understanding_payload("查询样本新闻", policy, NOW)
    assert "query_context" not in payload
    assert payload["temporal_context"]["server_now"] == NOW.isoformat()


def test_payload_rejects_naive_clock_and_non_boolean_optin(policy):
    with pytest.raises(ValueError, match="timezone"):
        catalog.build_query_understanding_payload("新闻", policy, datetime(2026, 10, 3))
    with pytest.raises(ValueError, match="boolean"):
        catalog.build_query_understanding_payload("新闻", policy, NOW, query_context_enabled="true")


def test_offline_payload_matches_online_context_and_preserves_time_request(policy):
    question = "查询昨天全天点击率最高的新闻"
    offline, _, clock = _payload(
        {"question": question, "policy": policy.model_dump(mode="json"), "now": NOW.isoformat()},
        query_context_enabled=True,
    )
    online = catalog.build_query_understanding_payload(question, policy, NOW, query_context_enabled=True)
    assert offline == online
    assert online["question"] == question
    assert clock == NOW
    assert online["query_context"]["capabilities"]["cross_hour_uv"] == "unsupported"
    assert any("全天和多小时" in rule for rule in online["query_context"]["semantics"])
    assert any("样本日期替换" in rule for rule in online["query_context"]["semantics"])


@pytest.mark.asyncio
async def test_online_adapter_passes_identical_optin_payload_to_model(policy):
    adapter = object.__new__(LocalConversationHotNewsQuery)
    adapter._policy = policy
    adapter._clock = lambda: NOW
    adapter._resolve_query = resolve_structured_query
    adapter._understanding_timeout = 2
    adapter._query_context_enabled = True
    context = catalog.build_query_context(policy)
    ready = context["examples"][0]
    adapter._understanding_agent = SimpleNamespace(run_structured=AsyncMock(return_value=SimpleNamespace(
        value=QueryUnderstanding.model_validate(ready["proposal"]), request_id="fixture-call",
    )))
    await adapter._understand(question=ready["question"], tenant_id="fixture-tenant")
    actual = adapter._understanding_agent.run_structured.await_args.kwargs["payload"]
    expected, _, _ = _payload({
        "question": ready["question"], "policy": policy.model_dump(mode="json"), "now": NOW.isoformat(),
    }, query_context_enabled=True)
    assert actual == expected


def test_portable_source_closure_pins_catalog_asset():
    from evaluation.run_query_semantics import source_closure
    sources = source_closure()
    assert sources["app/modules/conversation/query_catalog_v1.json"] == catalog.CATALOG_SHA256
    assert "app/modules/conversation/query_catalog.py" in sources


def test_cli_explicit_context_flag_reaches_runner(monkeypatch, capsys):
    from evaluation import run_query_semantics as cli
    from evaluation import query_semantics_runner as runner
    calls = []
    async def fake(*args, **kwargs):
        calls.append(kwargs)
        return {"status": "completed"}
    monkeypatch.setattr(runner, "run_predictions", fake)
    common = ["run", "--package", "fixture", "--endpoint", "fixture.json",
              "--out", "out", "--max-calls", "0", "--max-seconds", "1"]
    assert cli.main(common) == 0
    assert cli.main([*common, "--query-context"]) == 0
    assert calls == [{"query_context_enabled": False}, {"query_context_enabled": True}]


def test_existing_base_lora_comparison_rejects_different_query_context(monkeypatch, tmp_path):
    from evaluation import query_semantics_scoring as scoring
    baseline, candidate = tmp_path / "base", tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    identity = {
        "base_model": "fixture-model", "base_revision": "fixed",
        "tokenizer_revision": "fixed", "chat_template_sha256": "a" * 64,
        "quantization": "fp32", "role": "base", "adapter_sha256": None,
        "training_group_ids": [],
    }
    common = {
        "identity": identity, "package_manifest_sha256": "b" * 64,
        "source_files_sha256": {"fixture.py": "c" * 64},
        "dependency_versions": {"python": "fixture"},
        "budget": {"max_calls": 1}, "endpoint_settings": {"timeout_seconds": 1},
    }
    (baseline / "run-spec.json").write_text(json.dumps(common), encoding="utf-8")
    tuned = {**common, "identity": {**identity, "role": "lora",
             "adapter_sha256": "d" * 64, "training_group_ids": ["train-fixture"]},
             "query_context": {"enabled": True, **catalog.query_context_identity()}}
    (candidate / "run-spec.json").write_text(json.dumps(tuned), encoding="utf-8")
    monkeypatch.setattr(scoring, "_score", lambda *a: {
        "resume_eligible": True, "metrics": {}, "evaluation_group_ids": [], "blocking_reasons": [],
    })
    report = scoring.compare_runs(tmp_path, baseline, candidate, tmp_path / "score")
    assert "run_contract_mismatch:query_context" in report["blocking_reasons"]
    assert report["exploratory_delta_percentage_points"] is None


def test_online_audit_exposes_only_context_identity_when_enabled():
    adapter = object.__new__(LocalConversationHotNewsQuery)
    assert adapter._query_context_audit() == {}
    adapter._query_context_enabled = True
    metadata = adapter._query_context_audit()["query_context"]
    assert metadata == {"enabled": True, **catalog.query_context_identity()}
    assert "metrics" not in metadata and "examples" not in metadata
