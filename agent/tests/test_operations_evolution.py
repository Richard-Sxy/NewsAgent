"""News evolution contracts, scope, leakage, scoring and offline execution."""

from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from app.modules.data_loop.evaluation.hot_news import HotNewsEvaluationService, load_evaluation_dataset
from app.model_runtime.core import RawInferenceResult
from app.model_runtime.result import AgentResult
from app.modules.hot_news.contracts.hot_news import HotNewsAnalysisReport
from evaluation.operations_evolution import (
    OperationsEvolutionCatalog, OperationsEvolutionDataset,
    analysis_projection, blind_input, build_dataset, load_catalog, summary,
    verify_artifact_hashes, write_artifacts,
)
from evaluation.operations_evolution_eval import (
    EvolutionPrediction, GatePolicy, ModelDecision, NativeEvolutionReviewRunner,
    compare_reports, run_reviews, score_predictions, select_cases,
)
from evaluation.operations_evolution_seed import build_catalog


@pytest.fixture(scope="module")
def dataset():
    return build_dataset(load_catalog())


def prediction(case):
    return EvolutionPrediction(
        case_id=case.case_id, scope_sha256=case.scope_sha256,
        decision_type=case.expected.decision_type,
        operator_feedback={"category": case.expected.category,
                           "reason_codes": case.expected.required_reason_codes},
        blocked=case.expected.must_block,
        evidence_news_ids=case.analysis_case.expected.required_evidence_news_ids,
        revision_fields=case.expected.required_revision_fields,
        revision=case.expected.revision,
    )


def test_catalog_is_reproducible_and_full_newsroom_coverage(dataset):
    assert load_catalog() == OperationsEvolutionCatalog.model_validate(build_catalog())
    stats = summary(dataset)
    assert (stats["case_count"], stats["board_count"], stats["operator_count"],
            stats["independent_story_count"], stats["unique_news_snapshot_count"]) == (432, 12, 12, 36, 216)
    assert stats["splits"] == {"development": 144, "test": 144, "validation": 144}
    assert dataset == build_dataset(load_catalog())
    # The same stage has desk-specific conditions and different reference decisions.
    assert len({case.expected.decision_type for case in dataset.cases
                if case.stage_index == 2 and not case.expected.must_block}) == 1
    assert len({case.expected.decision_type for case in dataset.cases if case.stage_index == 2}) >= 3


@pytest.mark.parametrize("mutation", ["future", "order", "missing_stage", "split", "source", "same_fact"])
def test_authoring_rejects_future_or_corrupt_story_lineage(mutation):
    payload = build_catalog()
    story = payload["boards"][0]["stories"][0]
    if mutation == "future":
        story["stages"][0]["facts"][0]["available_at"] = "2026-10-20T00:00:00+00:00"
    elif mutation == "order":
        story["stages"][0], story["stages"][1] = story["stages"][1], story["stages"][0]
    elif mutation == "missing_stage":
        story["stages"].pop()
    elif mutation == "split":
        story["split"] = "test"
    elif mutation == "source":
        story["stages"][0]["facts"][0]["source_id"] = "unknown"
    else:
        other = deepcopy(story["stages"][0]["facts"][0])
        other["text"] = "原fact身份被偷偷改写"
        story["stages"][1]["facts"].append(other)
    with pytest.raises(ValidationError):
        OperationsEvolutionCatalog.model_validate(payload)


@pytest.mark.parametrize("mutation", ["future_fact", "future_evidence", "split", "actor", "duplicate",
                                    "news", "ctr", "score", "fresh", "forged_approval"])
def test_saved_dataset_cannot_bypass_contract_validation(dataset, mutation):
    payload = dataset.model_dump(mode="json")
    case = payload["cases"][0]
    if mutation == "future_fact":
        case["known_facts"][0]["available_at"] = "2026-10-20T00:00:00+00:00"
    elif mutation == "future_evidence":
        case["analysis_case"]["analysis_input"]["related_news"][0]["published_at"] = "2026-10-20T00:00:00+00:00"
    elif mutation == "split":
        case["split"] = "test"
    elif mutation == "actor":
        case["scope"]["user_id"] = payload["cases"][-1]["scope"]["user_id"]
    elif mutation == "duplicate":
        payload["cases"].append(deepcopy(case))
    elif mutation == "news":
        case["analysis_case"]["analysis_input"]["news_id"] = payload["cases"][-1]["analysis_case"]["analysis_input"]["news_id"]
    elif mutation == "ctr":
        case["analysis_case"]["analysis_input"]["metrics"]["ctr"] += 0.01
    elif mutation == "score":
        case["analysis_case"]["analysis_input"]["hot_score"] += 1
    elif mutation == "fresh":
        case["expected"]["layer"] = "fresh_bad_case"
    else:
        case["annotation_status"] = "human_approved"
    with pytest.raises(ValidationError):
        OperationsEvolutionDataset.model_validate(payload)


def test_blind_inputs_have_only_current_facts_and_no_reference_labels(dataset):
    forbidden = {"expected", "revision", "required_reason_codes", "optional_reason_codes", "layer", "split",
                 "source_story_id", "source_board_id", "source_operator_id", "candidate_key", "analysis_verdict"}
    def walk(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    inputs = blind_input(dataset)
    walk(inputs)
    assert len(inputs["cases"]) == 432
    first = inputs["cases"][0]
    assert len(first["history"]) == 1
    assert len(blind_input(dataset.cases[10])["history"]) == 6


def test_fixed_denominator_and_stratified_sequence_scores(dataset):
    predictions = [prediction(case) for case in dataset.cases]
    report = score_predictions(dataset, predictions)
    assert report["pass_rate"] == 1
    assert report["sequence"]["passed_story_count"] == 36
    assert len(report["breakdown"]["by_board"]) == 12
    assert len(report["breakdown"]["by_stage"]) == 6
    assert len(report["breakdown"]["by_operator"]) == 12
    assert all(group["total"] == 36 for group in report["breakdown"]["by_board"].values())
    partial = score_predictions(dataset, predictions[:1])
    assert partial["total"] == 432 and partial["missing"] == 431
    assert partial["pass_rate"] == pytest.approx(1 / 432)
    assert partial["sequence"]["passed_story_count"] == 0
    subset = select_cases(dataset, board_id="finance", split="test")
    filtered = score_predictions(dataset, [prediction(case) for case in subset], board_id="finance", split="test")
    assert filtered["total"] == 12 and filtered["pass_rate"] == 1
    assert set(filtered["breakdown"]["by_board"]) == {"finance"}


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "scope", "unselected"])
def test_prediction_ids_and_scopes_are_fail_closed(dataset, mutation):
    predicted = prediction(dataset.cases[0])
    args = {}
    if mutation == "duplicate":
        predictions = [predicted, predicted]
    elif mutation == "unknown":
        predictions = [predicted.model_copy(update={"case_id": "unknown"})]
    elif mutation == "scope":
        predictions = [predicted.model_copy(update={"scope_sha256": "0" * 64})]
    else:
        predictions = [predicted]
        args["board_id"] = "finance"
    with pytest.raises(ValueError):
        score_predictions(dataset, predictions, **args)


def test_optional_reasons_and_alternative_decisions_are_not_unfairly_penalized(dataset):
    case = next(case for case in dataset.cases if case.expected.optional_reason_codes)
    payload = prediction(case).model_dump(mode="json")
    payload["operator_feedback"]["reason_codes"] += list(case.expected.optional_reason_codes)
    report = score_predictions(dataset, [EvolutionPrediction.model_validate(payload)])
    row = next(row for row in report["cases"] if row["case_id"] == case.case_id)
    assert row["passed"] and row["reason_precision"] == 1
    case = next(case for case in dataset.cases if len(case.expected.allowed_decisions) == 2)
    payload = prediction(case).model_dump(mode="json")
    payload["decision_type"] = "rejected"
    row = next(row for row in score_predictions(dataset, [EvolutionPrediction.model_validate(payload)])["cases"] if row["case_id"] == case.case_id)
    assert row["passed"]


@pytest.mark.parametrize("mutation", ["unsafe_accept", "evidence", "reason", "revision"])
def test_targeted_decision_errors_are_visible(dataset, mutation):
    case = next(case for case in dataset.cases if case.expected.decision_type == "corrected")
    payload = prediction(case).model_dump(mode="json")
    if mutation == "unsafe_accept":
        payload.update(decision_type="accepted", blocked=False, revision=None, revision_fields=[])
    elif mutation == "evidence":
        payload["evidence_news_ids"] = ["future-or-unprovided-evidence"]
    elif mutation == "reason":
        payload["operator_feedback"]["reason_codes"].append("other_with_explanation")
    else:
        payload["revision_fields"] = []
    row = next(row for row in score_predictions(dataset, [EvolutionPrediction.model_validate(payload)])["cases"] if row["case_id"] == case.case_id)
    assert not row["passed"]
    assert row["critical_failure"] is (mutation in {"unsafe_accept", "evidence"})
    if mutation == "reason":
        assert 0 < row["reason_precision"] < 1 and row["reason_recall"] == 1


def test_offline_gate_compares_all_strata_and_never_authorizes_activation(dataset):
    good = score_predictions(dataset, [prediction(case) for case in dataset.cases])
    assert compare_reports(good, good)["gate_passed"]
    assert compare_reports(good, good)["production_activation_authorized"] is False
    assert not compare_reports(good, good, policy=GatePolicy(require_previous=True))["gate_passed"]
    assert compare_reports(good, good, good, policy=GatePolicy(require_previous=True))["gate_passed"]
    assert not compare_reports(good, good, policy=GatePolicy(min_cases_per_cohort=37))["gate_passed"]
    worse = score_predictions(dataset, [prediction(case) for case in dataset.cases[12:]])
    gated = compare_reports(worse, good)
    assert not gated["gate_passed"]
    assert any(row["code"] == "baseline_regression" and row["scope"].startswith("by_story/") for row in gated["failures"])
    altered = deepcopy(good)
    altered["pass_rate"] = 0
    with pytest.raises(ValueError, match="aggregate"):
        compare_reports(altered, good)
    selected = select_cases(dataset, split="test")
    other = score_predictions(dataset, [prediction(case) for case in selected], split="test")
    with pytest.raises(ValueError, match="mismatch"):
        compare_reports(good, other)


def test_artifacts_rebuild_hashes_and_projections(dataset, tmp_path):
    write_artifacts(dataset, tmp_path)
    verify_artifact_hashes(tmp_path)
    write_artifacts(dataset, tmp_path)
    loaded = load_evaluation_dataset(tmp_path / "hot_news_analysis_test.json")
    assert len(loaded.cases) == 72
    assert loaded == analysis_projection(dataset, split="test")
    blind = tmp_path / "operations_inputs.json"
    blind.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        verify_artifact_hashes(tmp_path)
    with pytest.raises(ValueError, match="overwrite"):
        write_artifacts(dataset, tmp_path)


@pytest.mark.asyncio
async def test_native_runner_uses_blind_input_and_model_route_in_idempotency(dataset):
    case = dataset.cases[0]
    payload = prediction(case).model_dump(mode="json", exclude={"case_id", "scope_sha256"})
    requests = []
    class Port:
        async def complete(self, request):
            requests.append(request)
            return RawInferenceResult(content=json.dumps(payload), request_id="fixture")
    first = await NativeEvolutionReviewRunner(Port(), model_route="model-a").run(blind_input(case))
    await NativeEvolutionReviewRunner(Port(), model_route="model-b").run(blind_input(case))
    assert isinstance(first, ModelDecision)
    assert requests[0].idempotency_key != requests[1].idempotency_key
    assert "expected" not in json.loads(requests[0].messages[1].content)
    assert requests[0].tenant_id == case.scope.tenant_id


@pytest.mark.asyncio
async def test_runner_failures_remain_missing_and_exception_bodies_are_not_saved(dataset):
    calls = []
    class FailingRunner:
        async def run(self, payload):
            calls.append(payload)
            raise RuntimeError("untrusted-private-response")
    result = await run_reviews(dataset, FailingRunner(), board_id="finance", split="test")
    assert len(calls) == 12 and result["score"]["missing"] == 12
    assert "untrusted-private-response" not in json.dumps(result)


@pytest.mark.asyncio
async def test_resume_skips_saved_successes_and_failures_and_saves_each_remaining_case(dataset):
    selected = select_cases(dataset, board_id="finance", split="test")
    by_case = {case.case_id: case for case in selected}
    calls, checkpoints = [], []
    class Runner:
        async def run(self, payload):
            calls.append(payload["case_id"])
            return ModelDecision.model_validate(prediction(by_case[payload["case_id"]]).model_dump(
                exclude={"case_id", "scope_sha256"}))
    result = await run_reviews(
        dataset, Runner(), board_id="finance", split="test",
        initial_predictions=[prediction(selected[0])],
        initial_errors=[{"case_id": selected[1].case_id, "error_type": "TimeoutError"}],
        on_case=lambda predictions, errors: checkpoints.append((len(predictions), len(errors))))
    assert calls == [case.case_id for case in selected[2:]]
    assert len(checkpoints) == 10 and checkpoints[-1] == (11, 1)
    assert result["score"]["missing"] == 1 and result["score"]["total"] == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["scope", "duplicate", "unselected", "diagnostic"])
async def test_invalid_resume_state_fails_before_any_model_call(dataset, mutation):
    selected = select_cases(dataset, board_id="finance", split="test")
    saved = prediction(selected[0])
    errors = []
    if mutation == "scope":
        saved = saved.model_copy(update={"scope_sha256": "f" * 64})
    elif mutation == "duplicate":
        errors = [{"case_id": saved.case_id, "error_type": "TimeoutError"}]
    elif mutation == "unselected":
        errors = [{"case_id": dataset.cases[0].case_id, "error_type": "TimeoutError"}]
    else:
        errors = [{"case_id": selected[1].case_id, "error_type": "raw private response body"}]
    class Runner:
        async def run(self, payload):
            pytest.fail("invalid checkpoint must not reach the model")
    with pytest.raises(ValueError):
        await run_reviews(dataset, Runner(), board_id="finance", split="test",
                          initial_predictions=[saved], initial_errors=errors)


@pytest.mark.asyncio
async def test_cli_checkpoint_resume_and_runtime_identity(dataset, tmp_path, monkeypatch):
    from evaluation.run_operations_evolution import _run_model, parser
    from app.model_runtime import http
    from app.model_runtime.config_file import ModelRuntimeConfig
    import yaml
    config = ModelRuntimeConfig.model_validate({
        "schema_version": 1,
        "prompts": [{"scene": "fixture", "version": "v1", "system_prompt": "fixture"}],
        "inference": {"provider": "openai_compatible", "model_routes": ["fixture-a", "fixture-b"],
                      "url": "https://model.example.invalid/v1/chat/completions",
                      "api_key_env": "OPERATIONS_EVAL_TEST_KEY"},
        "embedding": {"provider": "local", "model_routes": ["fixture-embedding"]}})
    config_path = tmp_path / "runtime.yml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    output = tmp_path / "experiment.json"
    args = parser().parse_args(["run-model", "--runtime-config", str(config_path),
                               "--board-id", "finance", "--output", str(output)])
    selection = {"board_id": "finance", "split": "test", "owner_user_id": None}
    selected = select_cases(dataset, **selection)
    by_case = {case.case_id: case for case in selected}
    calls, closed = [], []
    class Client:
        def __init__(self, endpoint, *, api_key):
            assert api_key == "fixture-only"
        async def complete(self, request):
            case_id = json.loads(request.messages[1].content)["case_id"]
            calls.append(case_id)
            if case_id == selected[1].case_id:
                raise TimeoutError("sensitive body must never be checkpointed")
            content = prediction(by_case[case_id]).model_dump_json(exclude={"case_id", "scope_sha256"})
            return RawInferenceResult(content=content, request_id="fixture")
        async def close(self):
            closed.append(True)
    monkeypatch.setenv("OPERATIONS_EVAL_TEST_KEY", "fixture-only")
    monkeypatch.setattr(http, "OpenAICompatibleInferenceClient", Client)
    first = await _run_model(dataset, args, selection)
    checkpoint = output.with_name(output.name + ".progress.json")
    assert len(calls) == 12 and first["score"]["missing"] == 1
    assert "sensitive body" not in checkpoint.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="checkpoint exists"):
        await _run_model(dataset, args, selection)
    args.resume = True
    second = await _run_model(dataset, args, selection)
    assert len(calls) == 12 and first["predictions"] == second["predictions"]
    assert len(closed) == 2
    args.model_route = "fixture-b"
    with pytest.raises(ValueError, match="differs"):
        await _run_model(dataset, args, selection)


@pytest.mark.asyncio
async def test_existing_hot_news_evaluator_scores_the_evolution_projection(dataset):
    projected = analysis_projection(dataset, split="test")
    by_news = {case.analysis_input.news_id: case for case in projected.cases}
    class ContractRunner:
        async def run(self, value):
            case = by_news[value.news_id]
            report = HotNewsAnalysisReport(
                news_id=value.news_id, trend_assessment="仅解释合成指标。",
                dominant_driver=case.expected.allowed_dominant_drivers[0],
                attention_reasons=[{"reason_type": "metric", "statement": "合成聚合观察。",
                                    "metric_keys": ["ctr", "hot_score"], "evidence_news_ids": [],
                                    "confidence": 1, "certainty": "observed"}],
                related_contexts=[], operation_suggestions=[],
                evidence_news_ids=list(case.expected.required_evidence_news_ids), applied_memory_ids=[],
                limitations=["合成输入，只验证评测器合同。"], overall_confidence=1)
            return AgentResult(value=report, request_id="contract-fixture", usage={}, raw_content=report.model_dump_json())
    scored = await HotNewsEvaluationService(ContractRunner()).evaluate(projected)
    assert scored.total_cases == 72 and scored.pass_rate == 1
