"""Deterministic query-budget and comparable-vector ranking contracts."""
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.retrieval.query_policy import QueryRetrievalPolicy, rank_candidates
from app.retrieval.tiered_vector import VectorSearchHit, VectorTier


def policy(**overrides):
    return QueryRetrievalPolicy(version="query-recall-test-v1", **overrides)


def plan(config=None, text="新闻报道", **overrides):
    return (config or policy()).plan(
        text, **({"limit": 10, "default_candidates": 20, "max_candidates": 100,
                  "available_tiers": tuple(VectorTier)} | overrides),
    )


def hit(news_id, score, *, chunk=None, tier=VectorTier.HOT, version="embedding-v1"):
    return VectorSearchHit(
        chunk_id=chunk or f"chunk-{news_id}", news_id=news_id, title=f"title-{news_id}",
        excerpt="untrusted excerpt", source_url=None,
        publish_time=datetime(2026, 10, 20, tzinfo=UTC), raw_score=score,
        tier=tier, content_version=1, embedding_version=version,
    )


def rank(hits, **overrides):
    return rank_candidates(
        hits, **({"embedding_version": "embedding-v1", "rrf_k": 60, "limit": 10} | overrides),
    )


def test_defaults_have_no_topic_preferences_and_keep_every_tier():
    cfg = policy()
    assert cfg.topic_rules == cfg.topic_intents == {}
    result = plan(cfg)
    assert result.intent == "general" and result.topic is None
    assert result.reason == "general_default"
    assert result.tier_limits == {tier: 20 for tier in VectorTier}


@pytest.mark.parametrize("text,intent,expected", [
    ("今天最新报道", "recent", (40, 20, 20)),
    ("刚刚发布实时消息", "recent", (40, 20, 20)),
    ("梳理历史背景和来龙去脉", "background", (20, 20, 40)),
    ("发展历程与演变", "background", (20, 20, 40)),
    ("今天讨论历史背景", "general", (20, 20, 20)),
])
def test_auto_intent_is_conservative_and_changes_only_budgets(text, intent, expected):
    result = plan(text=text)
    assert result.intent == intent
    assert tuple(result.tier_limits[tier] for tier in VectorTier) == expected
    if intent == "general":
        assert result.reason == "conflicting_intent_cues"


def test_explicit_intent_has_priority_over_time_cues_and_topic():
    cfg = policy(topic_rules={"research": ("专题",)}, topic_intents={"research": "background"})
    result = plan(cfg, "今天专题历史背景", intent="recent", topic="research")
    assert result.intent == "recent" and result.reason == "explicit_intent"
    assert result.topic == "research"


def test_unique_topic_is_a_budget_hint_and_explicit_time_cues_take_priority():
    cfg = policy(topic_rules={"ai": ("大模型", "LLM"), "sport": ("足球",)},
                 topic_intents={"ai": "background", "sport": "recent"})
    result = plan(cfg, "llm专题")
    assert (result.topic, result.intent, result.reason) == ("ai", "background", "unique_topic_rule")
    assert set(result.tier_limits) == set(VectorTier)
    assert plan(cfg, "最新大模型").intent == "recent"
    assert plan(cfg, "足球历史背景").intent == "background"
    assert plan(cfg, "专题", topic="ai").reason == "explicit_topic"
    assert plan(cfg, "大模型足球", topic="sport").intent == "recent"


def test_ambiguous_topic_keeps_general_without_arbitrary_first_match():
    cfg = policy(topic_rules={"a": ("专题",), "b": ("报告",)}, topic_intents={"a": "recent"})
    result = plan(cfg, "专题报告")
    assert result.intent == "general" and result.topic is None
    assert result.reason == "ambiguous_topic_rules"


def test_unknown_explicit_topic_and_intent_fail_closed():
    with pytest.raises(ValueError, match="unknown.*topic"):
        plan(topic="not-configured", intent="recent")
    with pytest.raises(ValueError, match="unknown.*intent"):
        plan(intent="emergency")


def test_unmapped_known_topic_is_general():
    cfg = policy(topic_rules={"a": ("专题",)})
    result = plan(cfg, "专题")
    assert result.topic == "a" and result.intent == "general"


def test_budget_caps_and_available_tiers_do_not_drop_the_requested_limit():
    result = plan(text="今天", limit=30, default_candidates=50, max_candidates=60,
                  available_tiers=(VectorTier.COLD, VectorTier.HOT, VectorTier.COLD))
    assert result.tier_limits == {VectorTier.COLD: 60, VectorTier.HOT: 60}
    assert all(30 <= budget <= 60 for budget in result.tier_limits.values())
    result = plan(policy(min_per_tier=1000), max_candidates=25)
    assert all(budget == 25 for budget in result.tier_limits.values())


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": True}, {"limit": 101}, {"default_candidates": 0},
    {"max_candidates": False}, {"available_tiers": ()}, {"available_tiers": ("unknown",)},
])
def test_invalid_plan_bounds_are_rejected(kwargs):
    with pytest.raises(ValueError):
        plan(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"version": ""}, {"version": " spaced "}, {"candidate_multiplier": True},
    {"candidate_multiplier": 9}, {"min_per_tier": 0}, {"fusion": "rrf"}, {"unknown": 1},
    {"intent_multipliers": {"general": {tier: 1 for tier in VectorTier}}},
    {"intent_multipliers": {name: {VectorTier.HOT: 1} for name in ("general", "recent", "background")}},
    {"topic_rules": {"a": ()}}, {"topic_rules": {" a": ("x",)}},
    {"topic_rules": {"a": ("",)}}, {"topic_rules": {"a": ("LLM", "llm")}},
    {"topic_intents": {"missing": "recent"}},
])
def test_invalid_policy_is_rejected(kwargs):
    with pytest.raises(ValidationError):
        QueryRetrievalPolicy.model_validate({"version": "test"} | kwargs)


def test_nested_multipliers_are_strict_and_bounded():
    values = policy().model_dump()
    for invalid in (True, 0, 9, float("nan"), float("inf")):
        values["intent_multipliers"]["general"][VectorTier.HOT] = invalid
        with pytest.raises(ValidationError):
            QueryRetrievalPolicy.model_validate(values)


def test_global_similarity_removes_independent_tier_rank_bias():
    hot = [hit(f"hot-{i:02}", .3 - i * .001) for i in range(30)]
    cold = hit("cold-relevant", .95, tier=VectorTier.COLD)
    ranked, scores, details = rank(hot + [cold], limit=3)
    assert [item.news_id for item in ranked] == ["cold-relevant", "hot-00", "hot-01"]
    assert scores["cold-relevant"] == 1
    assert scores["hot-00"] == pytest.approx(61 / 62)
    assert details["mode"] == "global_similarity"
    assert details["score_kind"] == "global_rank_feature"
    assert details["returned_news"] == 3


def test_best_chunk_represents_news_and_equal_scores_have_stable_identity_order():
    candidates = [
        hit("b", .7), hit("a", .6, chunk="low-a"),
        hit("a", .9, chunk="z-best-a"), hit("a", .9, chunk="a-best-a"),
        hit("c", .7),
    ]
    forward = rank(candidates)
    backward = rank(list(reversed(candidates)))
    assert forward == backward
    ranked, _, details = forward
    assert [item.news_id for item in ranked] == ["a", "b", "c"]
    assert ranked[0].chunk_id == "a-best-a"
    assert details["duplicate_chunks"] == 2 and details["unique_news"] == 3


def test_exclusions_precede_dedup_and_scores_only_cover_returned_news():
    ranked, scores, details = rank(
        [hit("excluded", 2), hit("a", -2), hit("b", -4)],
        exclude_news_ids=("excluded",), limit=1,
    )
    assert [item.news_id for item in ranked] == ["a"]
    assert scores == {"a": 1.0}
    assert details["excluded_chunks"] == 1 and details["unique_news"] == 2
    assert "untrusted excerpt" not in str(details)
    assert rank([])[0:2] == ([], {})


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), True])
def test_nonfinite_or_boolean_similarity_is_rejected(invalid):
    with pytest.raises(ValueError, match="finite"):
        rank([hit("a", invalid)])


def test_embedding_drift_is_rejected_including_excluded_candidates():
    with pytest.raises(ValueError, match="embedding version drift"):
        rank([hit("a", .8, version="other")], exclude_news_ids=("a",))


@pytest.mark.parametrize("kwargs", [
    {"rrf_k": 0}, {"rrf_k": True}, {"limit": 0}, {"limit": 1.2}, {"embedding_version": ""},
])
def test_rank_argument_bounds_are_rejected(kwargs):
    with pytest.raises(ValueError):
        rank([], **kwargs)


def test_rank_feature_is_independent_of_tier_and_unbounded_ip_magnitude():
    original = [hit("a", 10000), hit("b", -1000)]
    changed = [replace(original[0], raw_score=.8, tier=VectorTier.COLD),
               replace(original[1], raw_score=.1, tier=VectorTier.WARM)]
    assert rank(original)[1] == rank(changed)[1] == {"a": 1, "b": 61 / 62}
