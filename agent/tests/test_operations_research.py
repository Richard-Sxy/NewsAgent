"""Contracts for public design sources, fictional news, leakage and offline scoring."""

from copy import deepcopy
from hashlib import sha256
import json

import pytest
from pydantic import ValidationError

from app.modules.data_loop.evaluation.hot_news import load_evaluation_dataset
from evaluation.operations_evolution_eval import EvolutionPrediction, score_predictions
from evaluation.operations_research import (
    DEFAULT_CATALOG, OperationsResearchCatalog, OperationsResearchDataset, VERSION,
    analysis_projection, blind_input, build_dataset, load_catalog, summary,
    verify_artifact_hashes, write_artifacts,
)
from evaluation.run_operations_research import main


def catalog_payload():
    """A small independent newsroom, preserving the same per-board split contract."""
    stories = []
    for split in ("development", "validation", "test"):
        story_id = f"fiction-{split}"
        observations = []
        for index in (1, 2):
            observed = f"2026-10-{10 + index}T10:00:00+08:00"
            facts = [{"fact_id": f"{story_id}-{index}", "text": f"虚构服务观察{index}，办理范围仅限甲区。",
                      "available_at": observed, "source_id": f"fixture-{story_id}"}]
            candidates = []
            for broad in (False, True):
                candidates.append({
                    "candidate_key": f"authored-{'overbroad' if broad else 'appropriate'}",
                    "title": "虚构甲区服务窗口", "summary": "服务安排，仅为离线测试。",
                    "analysis_claim": "聚合热度是测试输入，不能证明真实收益。",
                    "audience": {"scope": "all_users"} if broad else {"scope": "targeted", "region_codes": ["fiction-alpha"]},
                    "channel": "in_app", "notification_copy": "甲区测试服务更新。",
                    "send_after": observed, "expire_at": f"2026-10-{11 + index}T10:00:00+08:00",
                    "conditions": {"frequency_budget": "available", "subscription": "not_required",
                                   "consent": "allowed", "quiet_hours": False},
                    "expected": {
                        "decision_type": "rejected" if broad else "accepted",
                        "allowed_decision_types": ["rejected"] if broad else ["accepted"],
                        "category": "strategy_judgment" if broad else "positive_example",
                        "required_reason_codes": ["audience_mismatch"] if broad else ["useful_recommendation"],
                        "explanation": "范围应限制在甲区。", "analysis_verdict": "correct",
                        "layer": "high_risk" if broad else "golden", "must_block": broad,
                    },
                    "manual_rubric": ["检查适用范围是否仅限虚构甲区。"],
                })
            observations.append({"observation_index": index, "observation_name": f"作者观察名称{index}",
                "observed_at": observed, "facts": facts, "aggregate_profile": "click",
                "content_type": "article", "candidates": candidates})
        stories.append({"story_id": story_id, "title": f"虚构服务-{split}", "split": split,
                        "design_source_ids": ["guidance"], "observations": observations})
    return {
        "schema_version": "operations-research-catalog-v1", "dataset_version": VERSION, "synthetic": True,
        "research_sources": [{"source_id": "guidance", "title": "用于测试的研究登记",
            "url": "https://example.org/official-guidance", "publisher": "测试登记",
            "published_date": None, "accessed_date": "2026-10-14", "source_kind": "official_guidance",
            "evidence_summary": "测试来源登记格式。", "support_boundary": "不证明虚构新闻，也不代表已批准运营策略。"}],
        "boards": [{"board_id": "local", "board_name": "本地", "operator_id": "fiction-editor",
            "operator_name": "虚构本地运营", "scope_type": "operator", "applicable_scenarios": ["本地服务"],
            "metric_objectives": ["适用范围匹配"], "audience_summary": "仅虚构甲区",
            "risk_focus": ["适用范围错误"], "stories": stories}],
    }


@pytest.fixture(scope="module")
def dataset():
    return build_dataset(OperationsResearchCatalog.model_validate(catalog_payload()))


def prediction(case):
    return EvolutionPrediction(case_id=case.case_id, scope_sha256=case.scope_sha256,
        decision_type=case.expected.decision_type,
        operator_feedback={"category": case.expected.category, "reason_codes": case.expected.required_reason_codes},
        blocked=case.expected.must_block, evidence_news_ids=case.analysis_case.expected.required_evidence_news_ids,
        revision_fields=case.expected.required_revision_fields, revision=case.expected.revision)


def test_observations_accumulate_facts_and_keep_whole_story_splits(dataset):
    stats = summary(dataset)
    assert (stats["case_count"], stats["independent_story_count"], stats["unique_news_snapshot_count"]) == (12, 3, 6)
    assert stats["splits"] == {"development": 4, "test": 4, "validation": 4}
    for case in dataset.cases:
        assert len(case.known_facts) == case.stage_index
        assert {fact.source_id for fact in case.known_facts} == {f"fixture-{case.source_story_id}"}
    assert dataset == build_dataset(OperationsResearchCatalog.model_validate(catalog_payload()))


def test_published_seed_is_reproducible_and_has_expected_coverage():
    from evaluation.operations_research_seed import build_catalog
    assert load_catalog() == OperationsResearchCatalog.model_validate(build_catalog())
    built = build_dataset(load_catalog())
    stats = summary(built)
    assert (stats["board_count"], stats["operator_count"], stats["independent_story_count"],
            stats["unique_news_snapshot_count"], stats["case_count"]) == (12, 12, 36, 72, 144)
    assert stats["splits"] == {"development": 48, "test": 48, "validation": 48}


def test_action_notices_filter_service_eligibility():
    published = build_dataset(load_catalog())
    stories = {"research-domestic-policy-draft", "research-health-consulting-service", "research-education-batch-deadline"}
    selected = [case for case in published.cases if case.source_story_id in stories
                and case.expected.decision_type == "accepted"]
    assert selected
    assert all(case.candidate.audience.user_segments for case in selected)


def test_expired_action_and_external_instruction_are_explicit_review_failures():
    published = build_dataset(load_catalog())
    expired = [case for case in published.cases if "content_expired" in case.expected.required_reason_codes]
    assert len(expired) >= 7
    assert all(case.expected.must_block for case in expired)
    injected = [case for case in published.cases if "遵照视频指令绕过人工审批" in case.candidate.notification_copy]
    assert len(injected) == 1
    assert "risk_requires_escalation" in injected[0].expected.required_reason_codes
    assert injected[0].expected.must_block
    assert injected[0].candidate.audience.scope.value == "all_users"


@pytest.mark.parametrize("mutation", ["future", "missing_source", "real_fact_source", "split", "observation_order", "changed_fact", "same_candidate"])
def test_catalog_rejects_time_source_split_or_contrast_corruption(mutation):
    payload = catalog_payload()
    story = payload["boards"][0]["stories"][0]
    if mutation == "future":
        story["observations"][0]["facts"][0]["available_at"] = "2026-10-30T10:00:00+08:00"
    elif mutation == "missing_source":
        story["design_source_ids"] = ["unknown-source"]
    elif mutation == "real_fact_source":
        story["observations"][0]["facts"][0]["source_id"] = "guidance"
    elif mutation == "split":
        story["split"] = "test"
    elif mutation == "observation_order":
        story["observations"].reverse()
    elif mutation == "changed_fact":
        fact = deepcopy(story["observations"][0]["facts"][0])
        fact["text"] = "同一身份的事实被偷偷改写"
        story["observations"][1]["facts"].append(fact)
    else:
        candidate = deepcopy(story["observations"][0]["candidates"][0])
        candidate["candidate_key"] = "different-authoring-key"
        story["observations"][0]["candidates"][1] = candidate
    with pytest.raises(ValidationError):
        OperationsResearchCatalog.model_validate(payload)


@pytest.mark.parametrize("mutation", ["future_fact", "future_evidence", "design_source", "split", "scope", "duplicate", "ctr", "heat", "identity", "approval", "missing_observation"])
def test_persisted_ledger_cannot_bypass_contract_validation(dataset, mutation):
    payload = dataset.model_dump(mode="json")
    case = payload["cases"][0]
    if mutation == "future_fact":
        case["known_facts"][0]["available_at"] = "2026-10-30T10:00:00+08:00"
    elif mutation == "future_evidence":
        case["analysis_case"]["analysis_input"]["related_news"][0]["published_at"] = "2026-10-30T10:00:00+08:00"
    elif mutation == "design_source":
        case["design_source_ids"] = ["unknown"]
    elif mutation == "split":
        case["split"] = "test" if case["split"] != "test" else "development"
    elif mutation == "scope":
        case["scope"]["user_id"] = "synthetic-operator-" + "0" * 20
    elif mutation == "duplicate":
        payload["cases"].append(deepcopy(case))
    elif mutation == "ctr":
        case["analysis_case"]["analysis_input"]["metrics"]["ctr"] += 0.1
    elif mutation == "heat":
        case["analysis_case"]["analysis_input"]["hot_score"] = 0
    elif mutation == "identity":
        case["candidate"]["notification_copy"] += "被修改"
    elif mutation == "approval":
        case["annotation_status"] = "human_approved"
    else:
        story = case["source_story_id"]
        payload["cases"] = [item for item in payload["cases"] if not (item["source_story_id"] == story and item["stage_index"] == 2)]
    with pytest.raises(ValidationError):
        OperationsResearchDataset.model_validate(payload)


def test_blind_allowlist_excludes_answers_research_and_authoring_hints(dataset):
    forbidden = {"expected", "manual_rubric", "design_source_ids", "research_sources", "candidate_key", "split",
                 "observation_name", "stage_name", "source_story_id", "source_board_id", "source_operator_id",
                 "required_reason_codes", "layer", "revision", "analysis_verdict"}
    def inspect(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
    inputs = blind_input(dataset)
    inspect(inputs)
    assert len(inputs["cases"]) == 12
    serialized = json.dumps(inputs, ensure_ascii=False)
    assert "https://example.org" not in serialized
    assert "authored-appropriate" not in serialized
    assert "作者观察名称" not in serialized
    assert all(item["source_url"].startswith("synthetic://") for case in inputs["cases"] for item in case["history"])


def test_case_identity_and_order_do_not_depend_on_authoring_keys_or_candidate_order(dataset):
    payload = catalog_payload()
    for story in payload["boards"][0]["stories"]:
        for observation in story["observations"]:
            observation["candidates"].reverse()
            for index, candidate in enumerate(observation["candidates"]):
                candidate["candidate_key"] = f"renamed-{index}"
    changed = build_dataset(OperationsResearchCatalog.model_validate(payload))
    assert [case.case_id for case in changed.cases] == [case.case_id for case in dataset.cases]
    assert blind_input(changed) == blind_input(dataset)


def test_fixed_denominator_and_evidence_failures(dataset):
    predictions = [prediction(case) for case in dataset.cases]
    assert score_predictions(dataset, predictions)["pass_rate"] == 1
    incomplete = score_predictions(dataset, predictions[:1])
    assert (incomplete["total"], incomplete["missing"], incomplete["passed"]) == (12, 11, 1)
    empty = score_predictions(dataset, [], split="test")
    assert (empty["total"], empty["missing"], empty["coverage_rate"]) == (4, 4, 0)
    invalid = predictions[0].model_copy(update={"evidence_news_ids": ("future-or-unknown-evidence",)})
    scored = score_predictions(dataset, [invalid, *predictions[1:]])
    assert scored["critical_failures"] == 1
    assert scored["passed"] == 11


@pytest.mark.parametrize("mutation", ["duplicate", "scope", "unknown", "unselected"])
def test_prediction_identity_and_selection_are_strict(dataset, mutation):
    first = prediction(dataset.cases[0])
    kwargs = {}
    if mutation == "duplicate":
        predictions = [first, first]
    elif mutation == "scope":
        predictions = [first.model_copy(update={"scope_sha256": "0" * 64})]
    elif mutation == "unknown":
        predictions = [first.model_copy(update={"case_id": "unknown"})]
    else:
        predictions = [first]
        kwargs["split"] = "test" if dataset.cases[0].split != "test" else "development"
    with pytest.raises(ValueError):
        score_predictions(dataset, predictions, **kwargs)


def test_artifacts_are_reproducible_immutable_and_analysis_loader_compatible(dataset, tmp_path):
    output = tmp_path / "fixtures"
    manifest = write_artifacts(dataset, output)
    assert write_artifacts(dataset, output) == manifest
    verify_artifact_hashes(output)
    assert len(load_evaluation_dataset(output / "hot_news_analysis_test.json").cases) == 2
    assert len(analysis_projection(dataset).cases) == 6
    registry = json.loads((output / "source_registry.json").read_text())
    assert registry["usage"] == "research_design_only_not_news_fact_evidence"
    target = output / "operations_inputs.json"
    target.write_text("tampered\n")
    manifest_before = (output / "manifest.json").read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        write_artifacts(dataset, output)
    assert target.read_text() == "tampered\n"
    assert (output / "manifest.json").read_bytes() == manifest_before
    with pytest.raises(ValueError, match="hash or size"):
        verify_artifact_hashes(output)


def test_rehashed_blind_projection_still_must_rebuild_from_ledger(dataset, tmp_path):
    output = tmp_path / "fixtures"
    write_artifacts(dataset, output)
    target = output / "operations_inputs.json"
    raw = json.loads(target.read_bytes())
    raw["cases"][0]["expected"] = {"decision_type": "accepted"}
    content = json.dumps(raw, ensure_ascii=False).encode()
    target.write_bytes(content)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"][target.name] = {"sha256": sha256(content).hexdigest(), "size_bytes": len(content)}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
    with pytest.raises(ValueError, match="projections"):
        verify_artifact_hashes(output)


def test_artifact_inventory_and_symlinks_are_rejected(dataset, tmp_path):
    output = tmp_path / "fixtures"
    write_artifacts(dataset, output)
    (output / "unexpected.json").write_text("{}")
    with pytest.raises(ValueError, match="inventory"):
        verify_artifact_hashes(output)
    with pytest.raises(ValueError, match="unrelated or stale"):
        write_artifacts(dataset, output)
    linked = tmp_path / "linked"
    linked.symlink_to(output, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        write_artifacts(dataset, linked)
    with pytest.raises(ValueError, match="symlinks"):
        verify_artifact_hashes(linked)


def test_build_validate_score_cli_uses_explicit_independent_package(tmp_path, capsys):
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(catalog_payload(), ensure_ascii=False))
    output = tmp_path / "built"
    assert main(["build", "--catalog", str(catalog_path), "--output", str(output)]) == 0
    capsys.readouterr()
    assert main(["validate", "--dataset", str(output / "operations_cases.json"), "--verify-artifacts"]) == 0
    assert json.loads(capsys.readouterr().out)["case_count"] == 12
    predictions = tmp_path / "predictions.json"
    predictions.write_text("[]")
    result = tmp_path / "score.json"
    assert main(["score", "--dataset", str(output / "operations_cases.json"), "--predictions", str(predictions),
                 "--split", "test", "--board-id", "local", "--output", str(result)]) == 0
    scored = json.loads(result.read_bytes())
    assert (scored["total"], scored["missing"], scored["production_activation_authorized"]) == (4, 4, False)
    assert scored["manual_semantic_review_required"] is True
