"""Exercise synthetic feedback contracts without human records or external services."""

from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from app.evaluation.hot_news import (
    HotNewsEvaluationService,
    load_evaluation_dataset,
)
from app.model_runtime.result import AgentResult
from app.schemas.hot_news import HotNewsAnalysisReport
from evaluation.operations_synthetic import (
    OperatorPrediction,
    ScenarioCatalog,
    SyntheticOperationsDataset,
    analysis_projection,
    build_dataset,
    score_predictions,
    summary,
    verify_artifact_hashes,
    write_artifacts,
)


def judgment(decision, category, codes, verdict="unassessed"):
    return {
        "decision_type": decision,
        "operator_feedback": {"category": category, "reason_codes": codes},
        "analysis_verdict": verdict,
        "reason": "合成运营判断，待人工复核",
        "rationale": "本测试只使用模拟事实，不代表真实团队意见或生产策略。",
        "revision": "删除无依据归因，保留可核对事实。" if decision == "corrected" else None,
    }


@pytest.fixture
def catalog_payload():
    expectations = (
        (
            judgment("accepted", "positive_example", ["analysis_supported", "useful_recommendation"], "correct"),
            judgment("rejected", "editorial_judgment", ["low_public_value", "audience_mismatch"]),
        ),
        (
            judgment("rejected", "analysis_error", ["insufficient_evidence", "fact_or_attribution_error"], "incorrect"),
            judgment("corrected", "analysis_error", ["fact_or_attribution_error"], "partially_correct"),
        ),
        (
            judgment("deferred", "strategy_judgment", ["risk_requires_escalation", "timing_inappropriate"]),
            judgment("corrected", "editorial_judgment", ["copy_misleading"]),
        ),
    )
    groups = []
    for index, layer in enumerate(("golden", "fresh_bad_case", "high_risk")):
        groups.append({
            "strategy_id": f"OPS-{index + 1:02d}",
            "pair_id": f"event-{index}",
            "proposed_layer": layer,
            "contrast": "同一事件在不同运营约束下需要不同判断。",
            "variants": [{
                "variant_id": f"variant-{variant}",
                "title": f"虚构城市情景 {index} 的运营变体 {variant}",
                "facts": [
                    f"这是虚构事件 {index}，与其他事件组无关。",
                    f"当前运营约束为变体 {variant}，尚未执行任何触达。",
                    "所有指标均为程序合成聚合值，无企业用户明细。",
                ],
                "candidate": {
                    "action": "提交运营审核",
                    "audience": "模拟目标受众",
                    "channel": "模拟站内渠道",
                    "copy": f"情景 {index} 变体 {variant} 的待审核文案。",
                    "analysis_claim": "只解释已提供事实和聚合指标。",
                },
                "expected": expectations[index][variant],
                "source_ids": ["fixture-source"],
            } for variant in range(2)],
        })
    return {
        "catalog_version": "operations-scenarios-v1",
        "sources": [{
            "source_id": "fixture-source",
            "title": "测试用模拟研究参考，非新闻事实来源",
            "url": "https://example.test/operations-fixture",
        }],
        "groups": groups,
    }


@pytest.fixture
def dataset(catalog_payload):
    return build_dataset(ScenarioCatalog.model_validate(catalog_payload))


@pytest.mark.parametrize("kind", ["positive_example", "editorial_judgment", "strategy_judgment"])
def test_fresh_rejects_business_judgments_and_positive_examples(catalog_payload, kind):
    catalog_payload["groups"][0]["proposed_layer"] = "fresh_bad_case"
    replacement = (
        judgment("accepted", kind, ["analysis_supported"], "correct")
        if kind == "positive_example"
        else judgment("rejected", kind, ["low_public_value"], "incorrect")
    )
    catalog_payload["groups"][0]["variants"][0]["expected"] = replacement
    with pytest.raises(ValidationError, match="cannot enter Fresh"):
        ScenarioCatalog.model_validate(catalog_payload)


def test_fresh_validation_also_applies_to_serialized_dataset(dataset):
    payload = dataset.model_dump(mode="json", by_alias=True)
    payload["cases"][2]["scenario"]["expected"]["operator_feedback"]["category"] = "editorial_judgment"
    with pytest.raises(ValidationError, match="ineligible Fresh"):
        SyntheticOperationsDataset.model_validate(payload)


@pytest.mark.parametrize(("target", "field", "value"), [
    ("dataset", "synthetic", False),
    ("dataset", "annotation_status", "human_approved"),
    ("dataset", "strategy_status", "production_policy"),
    ("case", "synthetic", False),
    ("case", "annotation_status", "human_approved"),
    ("case", "delivery_status", "delivered"),
])
def test_synthetic_review_and_delivery_statuses_cannot_be_forged(dataset, target, field, value):
    payload = dataset.model_dump(mode="json", by_alias=True)
    target_payload = payload if target == "dataset" else payload["cases"][0]
    target_payload[field] = value
    with pytest.raises(ValidationError):
        SyntheticOperationsDataset.model_validate(payload)


def test_event_variants_cannot_leak_across_layers_and_case_ids_are_unique(dataset):
    payload = dataset.model_dump(mode="json", by_alias=True)
    payload["cases"][1]["proposed_layer"] = "high_risk"
    with pytest.raises(ValidationError, match="event leaks"):
        SyntheticOperationsDataset.model_validate(payload)
    payload = dataset.model_dump(mode="json", by_alias=True)
    payload["cases"].append(deepcopy(payload["cases"][0]))
    with pytest.raises(ValidationError, match="duplicate synthetic case ID"):
        SyntheticOperationsDataset.model_validate(payload)


@pytest.mark.parametrize("component", ["ctr", "hot_score", "click", "consumption", "interaction", "growth"])
def test_persisted_ctr_total_and_each_component_are_recomputed(dataset, component):
    payload = dataset.model_dump(mode="json", by_alias=True)
    analysis = payload["cases"][0]["analysis_case"]["analysis_input"]
    if component == "ctr":
        analysis["metrics"]["ctr"] += 0.01
        error = "CTR differs"
    elif component == "hot_score":
        analysis["hot_score"] += 0.01
        error = "heat score/components differ"
    else:
        analysis["score_components"][component] += 0.01
        error = "heat score/components differ"
    with pytest.raises(ValidationError, match=error):
        SyntheticOperationsDataset.model_validate(payload)


@pytest.mark.parametrize("field", ["profile", "expected_driver"])
def test_driver_preconditions_cannot_be_changed_to_match_a_bad_prediction(dataset, field):
    payload = dataset.model_dump(mode="json", by_alias=True)
    if field == "profile":
        payload["cases"][0]["metric_profile"] = "consumption"
    else:
        payload["cases"][0]["analysis_case"]["expected"]["allowed_dominant_drivers"] = ["growth"]
    with pytest.raises(ValidationError, match="driver expectation differs"):
        SyntheticOperationsDataset.model_validate(payload)


@pytest.mark.parametrize("mutation", ["ctr", "funnel"])
def test_growth_baseline_is_validated_as_aggregate_input(catalog_payload, mutation):
    growth_group = deepcopy(catalog_payload["groups"][2])
    growth_group["strategy_id"] = "OPS-04"
    growth_group["pair_id"] = "event-growth"
    for variant in growth_group["variants"]:
        variant["title"] = "独立的虚构增长事件"
        variant["facts"][0] = "这是独立的虚构增长事件，不属于原事件。"
    catalog_payload["groups"].append(growth_group)
    growth_dataset = build_dataset(ScenarioCatalog.model_validate(catalog_payload))
    payload = growth_dataset.model_dump(mode="json", by_alias=True)
    baseline = payload["cases"][6]["baseline"]
    assert baseline is not None
    if mutation == "ctr":
        baseline["ctr"] = 0.2
        error = "CTR differs"
    else:
        baseline["clicks"] = baseline["impressions"] + 1
        error = "consumption funnel"
    with pytest.raises(ValidationError, match=error):
        SyntheticOperationsDataset.model_validate(payload)


def test_comparison_pair_cannot_be_reassigned_to_a_different_event(dataset):
    payload = dataset.model_dump(mode="json", by_alias=True)
    payload["cases"][1]["event_id"] = "different-event"
    with pytest.raises(ValidationError, match="one comparison pair"):
        SyntheticOperationsDataset.model_validate(payload)


def test_research_sources_must_be_defined_in_catalog_and_export(dataset, catalog_payload):
    catalog_payload["groups"][0]["variants"][0]["source_ids"] = ["unknown-source"]
    with pytest.raises(ValidationError, match="unknown research source"):
        ScenarioCatalog.model_validate(catalog_payload)
    payload = dataset.model_dump(mode="json", by_alias=True)
    payload["cases"][0]["scenario"]["source_ids"] = ["unknown-source"]
    with pytest.raises(ValidationError, match="unknown research source"):
        SyntheticOperationsDataset.model_validate(payload)


def test_build_and_artifacts_are_reproducible_and_static_loader_accepts_every_layer(catalog_payload, tmp_path):
    first = build_dataset(ScenarioCatalog.model_validate(catalog_payload))
    second = build_dataset(ScenarioCatalog.model_validate(deepcopy(catalog_payload)))
    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    first_manifest = write_artifacts(first, tmp_path / "first")
    second_manifest = write_artifacts(second, tmp_path / "second")
    assert first_manifest == second_manifest
    assert summary(first)["independent_event_groups"] == 3
    assert summary(first)["case_count"] == 6
    assert summary(first)["layers"] == {"golden": 2, "fresh_bad_case": 2, "high_risk": 2}
    for filename in (*first_manifest["files"], "manifest.json"):
        assert (tmp_path / "first" / filename).read_bytes() == (tmp_path / "second" / filename).read_bytes()
    for layer in (None, "golden", "fresh_bad_case", "high_risk"):
        filename = f"hot_news_analysis_{layer or 'all'}.json"
        loaded = load_evaluation_dataset(tmp_path / "first" / filename)
        assert loaded == analysis_projection(first, layer)
        assert len(loaded.cases) == (6 if layer is None else 2)


def test_blind_inputs_export_has_no_answers_revision_or_approval_fields(dataset, tmp_path):
    write_artifacts(dataset, tmp_path)
    inputs = json.loads((tmp_path / "operations_inputs.json").read_text(encoding="utf-8"))
    prohibited = {"expected", "verdict", "analysis_verdict", "revision", "operator_feedback", "label_approved_by"}

    def inspect(value):
        if isinstance(value, dict):
            assert not (set(value) & prohibited)
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(inputs)
    assert len(inputs["cases"]) == 6
    assert inputs["synthetic"] is True
    assert {case["case_id"] for case in inputs["cases"]} == {case.case_id for case in dataset.cases}


def predictions_for(dataset):
    return [OperatorPrediction(
        case_id=case.case_id,
        decision_type=case.scenario.expected.decision_type,
        operator_feedback=case.scenario.expected.operator_feedback,
    ) for case in dataset.cases]


def test_missing_predictions_count_as_failures_against_the_full_dataset(dataset):
    scores = score_predictions(dataset, predictions_for(dataset)[:1])
    assert scores["total"] == 6
    assert scores["missing"] == 5
    assert scores["pass_rate"] == pytest.approx(1 / 6)
    assert scores["decision_accuracy"] == pytest.approx(1 / 6)
    assert scores["category_accuracy"] == pytest.approx(1 / 6)
    assert scores["reason_codes_exact_match"] == pytest.approx(1 / 6)
    assert all(not row["passed"] for row in scores["cases"][1:])
    empty = score_predictions(dataset, [])
    assert empty["missing"] == 6
    assert empty["pass_rate"] == 0


def test_scoring_rejects_unknown_and_duplicate_prediction_ids(dataset):
    predictions = predictions_for(dataset)
    with pytest.raises(ValueError, match="duplicate prediction"):
        score_predictions(dataset, predictions + [predictions[0]])
    unknown = predictions[0].model_copy(update={"case_id": "unknown-case"})
    with pytest.raises(ValueError, match="unknown case_id"):
        score_predictions(dataset, [unknown])


@pytest.mark.parametrize("category", ["analysis_error", "editorial_judgment", "strategy_judgment", "positive_example"])
@pytest.mark.parametrize("mutation", ["missing_reason", "extra_reason"])
def test_every_category_requires_the_exact_reason_set(dataset, category, mutation):
    predictions = predictions_for(dataset)
    index = next(i for i, prediction in enumerate(predictions) if prediction.operator_feedback.category == category)
    payload = predictions[index].model_dump(mode="json")
    reasons = payload["operator_feedback"]["reason_codes"]
    assert len(reasons) >= 2
    payload["operator_feedback"]["reason_codes"] = reasons[1:] if mutation == "missing_reason" else reasons + ["other_with_explanation"]
    predictions[index] = OperatorPrediction.model_validate(payload)
    scored = score_predictions(dataset, predictions)
    assert scored["cases"][index]["decision_match"] is True
    assert scored["cases"][index]["category_match"] is True
    assert scored["cases"][index]["reason_codes_match"] is False
    assert scored["cases"][index]["passed"] is False
    assert scored["pass_rate"] == pytest.approx(5 / 6)


def test_reason_order_does_not_change_a_semantically_exact_match(dataset):
    predictions = predictions_for(dataset)
    payload = predictions[0].model_dump(mode="json")
    payload["operator_feedback"]["reason_codes"].reverse()
    predictions[0] = OperatorPrediction.model_validate(payload)
    assert score_predictions(dataset, predictions)["pass_rate"] == 1


def test_export_hash_check_detects_changed_blind_inputs(dataset, tmp_path):
    write_artifacts(dataset, tmp_path)
    verify_artifact_hashes(tmp_path)
    blind = tmp_path / "operations_inputs.json"
    payload = json.loads(blind.read_text(encoding="utf-8"))
    payload["cases"][0]["candidate"]["action"] = "篡改后的候选动作"
    blind.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact hash differs: operations_inputs.json"):
        verify_artifact_hashes(tmp_path)


def test_rebuild_cannot_leave_a_stale_layer_file(dataset, tmp_path):
    write_artifacts(dataset, tmp_path)
    payload = dataset.model_dump(mode="json", by_alias=True)
    payload["cases"] = [c for c in payload["cases"] if c["proposed_layer"] != "high_risk"]
    smaller = SyntheticOperationsDataset.model_validate(payload)
    with pytest.raises(ValueError, match="stale layer projection"):
        write_artifacts(smaller, tmp_path)


def test_variant_id_alone_does_not_make_a_comparison(catalog_payload):
    variant = deepcopy(catalog_payload["groups"][0]["variants"][0])
    variant["variant_id"] = "other-id-only"
    catalog_payload["groups"][0]["variants"][1] = variant
    with pytest.raises(ValidationError, match="comparison variants must differ"):
        ScenarioCatalog.model_validate(catalog_payload)


def report_for(case):
    expected = case.expected
    evidence = list(expected.required_evidence_news_ids)
    return HotNewsAnalysisReport(
        news_id=case.analysis_input.news_id,
        trend_assessment="仅解释合成聚合指标和输入证据。",
        dominant_driver=expected.allowed_dominant_drivers[0],
        attention_reasons=[{
            "reason_type": "metric",
            "statement": "引用已提供的聚合指标。",
            "metric_keys": list(expected.required_metric_keys),
            "evidence_news_ids": [],
            "confidence": 1,
            "certainty": "observed",
        }],
        related_contexts=[{"statement": "同一合成事件的事实记录。", "evidence_news_ids": evidence, "confidence": 1}],
        operation_suggestions=[],
        evidence_news_ids=evidence,
        applied_memory_ids=[],
        limitations=["合成数据及程序预期，仍需人工复核，不能证明真实运营效果。"],
        overall_confidence=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["none", "driver", "unknown_evidence", "missing_evidence", "metric", "limitation"])
async def test_analysis_scoring_passes_a_valid_report_and_rejects_targeted_errors(dataset, mutation):
    evaluation = analysis_projection(dataset).model_copy(update={"cases": (dataset.cases[0].analysis_case,)})
    case = evaluation.cases[0]
    payload = report_for(case).model_dump(mode="json")
    if mutation == "driver":
        payload["dominant_driver"] = "growth"
    elif mutation == "unknown_evidence":
        payload["evidence_news_ids"] = ["unprovided-evidence"]
    elif mutation == "missing_evidence":
        payload["evidence_news_ids"] = []
        payload["related_contexts"] = []
    elif mutation == "metric":
        payload["attention_reasons"][0]["metric_keys"] = ["hot_score"]
    elif mutation == "limitation":
        payload["limitations"] = []
    report = HotNewsAnalysisReport.model_validate(payload)

    class ReferenceRunner:
        async def run(self, analysis_input):
            assert analysis_input.news_id == report.news_id
            return AgentResult(value=report, request_id="synthetic-test", usage={}, raw_content=report.model_dump_json())

    scored = await HotNewsEvaluationService(ReferenceRunner()).evaluate(evaluation)
    result = scored.cases[0]
    assert result.passed is (mutation == "none")
    assert scored.pass_rate == (1 if mutation == "none" else 0)
    if mutation == "driver":
        assert result.contract_valid is True
        assert result.dominant_driver_match is False
    elif mutation == "unknown_evidence":
        assert result.contract_valid is False
    elif mutation == "missing_evidence":
        assert result.required_evidence_recall == 0
    elif mutation == "metric":
        assert result.required_metric_coverage == 0.5
    elif mutation == "limitation":
        assert result.limitation_match is False
