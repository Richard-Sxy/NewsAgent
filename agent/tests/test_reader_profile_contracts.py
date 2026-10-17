"""Policy-bound lookbacks and aggregate-only domain invariants."""
from copy import deepcopy

from pydantic import ValidationError
import pytest

from app.reader_profiles.contracts import PROFILE_POLICY_VERSION, ReaderProfileSummary


def payload(*, source_mode="synthetic", policy=PROFILE_POLICY_VERSION, long=720, short=24):
    interest = dict(profiled_reader_count=0, unknown_reader_count=0, cold_start_reader_count=0,
        baseline_profiled_count=0, topics=[])
    result = dict(news_id="news-1", source_mode=source_mode, taxonomy_version="topics-v1",
        profile_policy_version=policy, content_sha256="c" * 64, content_topic_ids=["tech"],
        reader_count=0, long_term=dict(interest, lookback_hours=long),
        short_term=dict(interest, lookback_hours=short), profile_cutoff="2026-10-14T08:00:00Z",
        trigger_article_excluded=True, count_method="exact_synthetic_distinct" if source_mode == "synthetic"
            else "enterprise_exact_distinct", error_bound=None, status="empty")
    if source_mode == "enterprise":
        result["identity_policy_version"] = "enterprise-user-id-tenant-scoped-v1"
    return result


@pytest.mark.parametrize("mode", ["synthetic", "enterprise"])
def test_current_policy_valid_lookbacks_roundtrip(mode):
    summary = ReaderProfileSummary.model_validate(payload(source_mode=mode))
    assert (summary.long_term.lookback_hours, summary.short_term.lookback_hours) == (720, 24)
    assert ReaderProfileSummary.model_validate_json(summary.model_dump_json()) == summary


@pytest.mark.parametrize("long,short", [(1, 24), (720, 8760), (24, 720), (720, 720), (24, 24)])
@pytest.mark.parametrize("mode", ["synthetic", "enterprise"])
def test_current_policy_rejects_wrong_or_swapped_lookbacks(long, short, mode):
    with pytest.raises(ValidationError, match="requires 720-hour long-term and 24-hour short-term"):
        ReaderProfileSummary.model_validate(payload(source_mode=mode, long=long, short=short))


def test_other_explicit_enterprise_policy_does_not_inherit_v1_horizons():
    result = ReaderProfileSummary.model_validate(payload(source_mode="enterprise",
        policy="enterprise-interest-policy-v2", long=168, short=12))
    assert (result.long_term.lookback_hours, result.short_term.lookback_hours) == (168, 12)


@pytest.mark.parametrize("missing", [True, False])
def test_enterprise_identity_policy_remains_explicit(missing):
    item = payload(source_mode="enterprise")
    if missing:
        item.pop("identity_policy_version")
    else:
        item["identity_policy_version"] = "synthetic-user-id-tenant-scoped-v1"
    with pytest.raises(ValidationError, match="explicit enterprise identity policy"):
        ReaderProfileSummary.model_validate(item)


@pytest.mark.parametrize("change", [
    lambda item: item.update(reader_count=-1),
    lambda item: item.update(status="complete"),
    lambda item: item["long_term"].update(cold_start_reader_count=1),
    lambda item: item["short_term"].update(unknown_reader_count=1),
    lambda item: item.update(recipient_ids=["personal-identity"]),
    lambda item: item.update(error_bound="0.1"),
])
def test_aggregate_counts_and_no_personal_output_contract(change):
    item = deepcopy(payload())
    change(item)
    with pytest.raises(ValidationError):
        ReaderProfileSummary.model_validate(item)


def nested_payload(*, mode="synthetic", policy=PROFILE_POLICY_VERSION, approximate=False):
    item = payload(source_mode=mode, policy=policy)
    item.update(reader_count=5, status="complete")
    interest = dict(lookback_hours=720, profiled_reader_count=5, unknown_reader_count=0,
        cold_start_reader_count=0, baseline_profiled_count=10,
        topics=[dict(topic_id="tech", reader_count=5, share_all="1.00000000", share_profiled="1.00000000",
            baseline_reader_count=5, baseline_profiled_count=10, baseline_share="0.50000000",
            lift="2.00000000", low_sample=False)])
    item["long_term"] = deepcopy(interest)
    item["short_term"] = dict(deepcopy(interest), lookback_hours=24)
    if approximate:
        item.update(count_method="enterprise_approx_distinct", error_bound="0.02")
    return item


@pytest.mark.parametrize("mode", ["synthetic", "enterprise"])
@pytest.mark.parametrize("dimension", ["long_term", "short_term"])
def test_current_exact_policy_rejects_smaller_baseline_population(mode, dimension):
    item = nested_payload(mode=mode)
    interest = item[dimension]
    interest.update(baseline_profiled_count=1, topics=[])
    with pytest.raises(ValidationError, match="contained in their baseline population"):
        ReaderProfileSummary.model_validate(item)


@pytest.mark.parametrize("mode", ["synthetic", "enterprise"])
@pytest.mark.parametrize("dimension", ["long_term", "short_term"])
def test_current_exact_policy_rejects_smaller_baseline_topic_population(mode, dimension):
    item = nested_payload(mode=mode)
    item[dimension]["topics"][0].update(baseline_reader_count=1, baseline_share="0.10000000",
        low_sample=True, lift=None)
    with pytest.raises(ValidationError, match="contained in their baseline topic population"):
        ReaderProfileSummary.model_validate(item)


@pytest.mark.parametrize("mode", ["synthetic", "enterprise"])
def test_current_exact_policy_accepts_equal_or_larger_baseline(mode):
    item = nested_payload(mode=mode)
    result = ReaderProfileSummary.model_validate(item)
    assert result.long_term.baseline_profiled_count == 10
    assert result.long_term.topics[0].baseline_reader_count == 5


@pytest.mark.parametrize("variant", ["approximate", "other_policy"])
def test_other_policy_or_approximation_does_not_inherit_exact_nesting(variant):
    item = nested_payload(mode="enterprise", approximate=variant == "approximate",
        policy="enterprise-interest-policy-v2" if variant == "other_policy" else PROFILE_POLICY_VERSION)
    for dimension in ("long_term", "short_term"):
        item[dimension].update(baseline_profiled_count=1)
        item[dimension]["topics"][0].update(baseline_reader_count=1, baseline_profiled_count=1,
            baseline_share="1.00000000", low_sample=True, lift=None)
    assert ReaderProfileSummary.model_validate(item).long_term.baseline_profiled_count == 1


@pytest.mark.parametrize("dimension", ["long_term", "short_term"])
def test_current_policy_rejects_lift_with_insufficient_baseline_tag_support(dimension):
    item = nested_payload(mode="enterprise", approximate=True)
    item[dimension]["topics"][0].update(baseline_reader_count=1, baseline_share="0.10000000",
        low_sample=False, lift="10.00000000")
    with pytest.raises(ValidationError, match="low-sample flag differs from the bound policy support"):
        ReaderProfileSummary.model_validate(item)


@pytest.mark.parametrize("dimension", ["long_term", "short_term"])
def test_current_policy_requires_low_sample_flag_even_when_lift_is_already_null(dimension):
    item = nested_payload(mode="enterprise", approximate=True)
    item[dimension]["topics"][0].update(baseline_reader_count=4, baseline_share="0.40000000",
        low_sample=False, lift=None)
    with pytest.raises(ValidationError, match="low-sample flag differs from the bound policy support"):
        ReaderProfileSummary.model_validate(item)


@pytest.mark.parametrize("dimension", ["long_term", "short_term"])
def test_current_policy_rejects_unjustified_low_sample_flag_at_support_threshold(dimension):
    item = nested_payload(mode="enterprise")
    item[dimension]["topics"][0].update(low_sample=True, lift=None)
    with pytest.raises(ValidationError, match="low-sample flag differs from the bound policy support"):
        ReaderProfileSummary.model_validate(item)


@pytest.mark.parametrize("dimension", ["long_term", "short_term"])
def test_current_policy_accepts_low_support_only_with_flag_and_suppressed_lift(dimension):
    item = nested_payload(mode="enterprise", approximate=True)
    item[dimension]["topics"][0].update(baseline_reader_count=4, baseline_share="0.40000000",
        low_sample=True, lift=None)
    summary = ReaderProfileSummary.model_validate(item)
    topic = getattr(summary, dimension).topics[0]
    assert topic.low_sample and topic.lift is None


def test_other_explicit_policy_does_not_inherit_v1_minimum_baseline_support():
    item = nested_payload(mode="enterprise", policy="enterprise-interest-policy-v2", approximate=True)
    item["long_term"]["topics"][0].update(baseline_reader_count=1, baseline_share="0.10000000",
        low_sample=False, lift="10.00000000")
    assert ReaderProfileSummary.model_validate(item).long_term.topics[0].lift == 10

