from copy import deepcopy
import json
from pathlib import Path

import pytest

from evaluation.report_pilot_scoring import (
    canonical_hash, make_gold_template, make_human_review, score_report_case,
    score_reports, validate_gold,
)


def approved(pack):
    pack.update(status="approved", reviewer="test-human", reviewed_at="2026-10-24T12:00:00+08:00")
    return pack


@pytest.fixture
def task():
    inputs = [{
        "case_id": "E01-REPORT", "event_group_id": "E01", "split": "development",
        "input": {"topic": "变化说明", "requirements": {"source_documents": [{
            "source_id": "S1", "url": "https://example.com/news", "content": "基期72%，本期90%。",
        }]}},
    }]
    checks = [
        {"check_id": "PP", "operation": "percentage_point_delta", "operands": ["NEW", "OLD"],
         "expected_value": "18", "unit": "个百分点", "tolerance": "0.01"},
        {"check_id": "REL", "operation": "relative_change_percent", "operands": ["NEW", "OLD"],
         "expected_value": "25", "unit": "%", "tolerance": "0.01"},
    ]
    base = {"required_fact_ids": ["OLD"], "numeric_checks": [],
            "forbidden_claims": ["称提升90%"], "required_caveats": ["注明报道来源"]}
    labels = [{
        "case_id": "E01-REPORT", "event_group_id": "E01", "split": "development",
        "stage_labels": {
            "research": {"expected": deepcopy(base)},
            "write": {"expected": {**deepcopy(base), "required_fact_ids": ["NEW"], "numeric_checks": checks}},
            # Reviewer probe gold deliberately differs and must not enter report gold.
            "review": {"expected": {**deepcopy(base), "required_fact_ids": ["REVIEW-ONLY"],
                                      "numeric_checks": [{"check_id": "PROBE-ONLY"}]}},
        },
        "source_fact_gold": [{"source_id": "S1", "facts": [
            {"fact_id": "OLD", "statement": "基期72%", "value": "72", "unit": "%",
             "evidence_locator": "content.chars[0:5]"},
            {"fact_id": "NEW", "statement": "本期90%", "value": "90", "unit": "%",
             "evidence_locator": "content.chars[6:11]"},
        ]}],
    }]
    return inputs, labels


def completed_trace(case_id="E01-REPORT", event_group_id="E01"):
    return {
        "case_id": case_id, "event_group_id": event_group_id, "split": "development",
        "status": "completed", "review_decision": "approve", "model_calls": 5,
        "stages": [{"stage": name, "status": "completed"}
                   for name in ("research", "outline", "section_01", "assemble", "review")],
        "outputs": {
            "research": {"facts": [{"fact_id": "F001", "claim": "基期72%，本期90%。",
                                     "evidence": "报道披露基期72%，本期90%。"}]},
            "outline": {"sections": [{"section_id": "S001"}]},
            "sections": [{"content": "章节内容"}],
            "draft": {"content": "任务解决率由72%升至90%，提高18个百分点，相对提高25%。"},
            "review": {"decision": "approve", "issues": []},
        },
    }


def fill_human(human, *, usable=True):
    for case in human["cases"]:
        for fact in case["fact_checks"]:
            fact.update(correct=True, output_quote="基期72%，本期90%。")
        for numeric in case["numeric_checks"]:
            if numeric["check_id"] == "PP":
                numeric.update(correct_context=True, observed_value="18", observed_unit="个百分点", output_quote="提高18个百分点")
            else:
                numeric.update(correct_context=True, observed_value="25", observed_unit="%", output_quote="提高25%")
        case.update(critical_errors=[], usable=usable, usable_note="流程失败未形成完整交付" if not usable else "")
        case["reviewer_assessment"].update(draft_has_major_error=False, detected_major_error=False,
                                           false_alarm=False, evidence_note="人工对照全文，Reviewer未报告严重错误。")
    return approved(human)


def evaluation(task, traces=None):
    inputs, labels = task
    gold = approved(make_gold_template(inputs, labels))
    traces = traces if traces is not None else [completed_trace()]
    human = fill_human(make_human_review(traces, gold, "run-01"))
    return gold, traces, human


def test_template_uses_report_gold_and_leaves_all_human_decisions_pending(task):
    inputs, labels = task
    gold = make_gold_template(inputs, labels)
    assert gold["status"] == "pending_human_review"
    assert gold["reviewer"] is None and gold["reviewed_at"] is None
    assert gold["cases"][0]["required_fact_ids"] == ["OLD", "NEW"]
    assert [row["check_id"] for row in gold["cases"][0]["numeric_checks"]] == ["PP", "REL"]
    assert gold["cases"][0]["source_facts"][0]["source_url"] == inputs[0]["input"]["requirements"]["source_documents"][0]["url"]
    assert "REVIEW-ONLY" not in json.dumps(gold)
    with pytest.raises(ValueError, match="pending human"):
        validate_gold(gold, inputs)
    validate_gold(gold, inputs, require_approved=False)
    human = make_human_review([completed_trace()], gold, "run-01")
    assert human["status"] == "pending_human_review"
    assert human["cases"][0]["fact_checks"][0]["correct"] is None
    assert human["cases"][0]["numeric_checks"][0]["correct_context"] is None
    assert human["cases"][0]["critical_errors"] is None
    assert human["cases"][0]["usable"] is None


def test_actual_three_report_labels_recompute_offline():
    source = Path(__file__).parents[1] / "evaluation/datasets/writing/report_pilot_v1"
    inputs = [json.loads(row) for row in (source / "inputs.jsonl").read_text().splitlines()]
    labels = [json.loads(row) for row in (source / "private/labels.draft.jsonl").read_text().splitlines()]
    gold = make_gold_template(inputs, labels)
    validate_gold(gold, inputs, require_approved=False)
    assert len(gold["cases"]) == 3
    assert [len(case["numeric_checks"]) for case in gold["cases"]] == [2, 4, 2]
    assert [len(case["required_fact_ids"]) for case in gold["cases"]] == [6, 6, 4]


@pytest.mark.parametrize("mutation,pattern", [
    (lambda g: g["cases"][0]["source_facts"].append(deepcopy(g["cases"][0]["source_facts"][0])), "duplicate"),
    (lambda g: g["cases"][0]["required_fact_ids"].append("MISSING"), "unknown fact"),
    (lambda g: g["cases"][0]["source_facts"][0].update(source_id="UNKNOWN"), "unknown source_id"),
    (lambda g: g["cases"][0]["source_facts"][0].update(locator="content.chars[0:999]"), "out of source bounds"),
    (lambda g: g["cases"][0]["numeric_checks"][0].update(tolerance="-1"), "nonnegative"),
    (lambda g: g["cases"][0]["numeric_checks"][0].update(expected_value="NaN"), "finite decimal"),
    (lambda g: g["cases"][0]["numeric_checks"][0].update(tolerance="Infinity"), "finite decimal"),
    (lambda g: g["cases"][0]["numeric_checks"][0].update(expected_value="19"), "recomputation"),
    (lambda g: g["cases"][0]["numeric_checks"][1].update(operands=["OLD", "NEW"]), "recomputation"),
    (lambda g: g["cases"][0]["numeric_checks"][0].update(unit="%"), "unit mismatch"),
])
def test_invalid_gold_is_blocked(task, mutation, pattern):
    inputs, labels = task
    gold = approved(make_gold_template(inputs, labels))
    mutation(gold)
    with pytest.raises(ValueError, match=pattern):
        validate_gold(gold, inputs)


def test_changed_inputs_or_case_order_do_not_reuse_gold_approval(task):
    inputs, labels = task
    gold = approved(make_gold_template(inputs, labels))
    changed = deepcopy(inputs)
    changed[0]["input"]["topic"] = "修改后的主题"
    with pytest.raises(ValueError, match="inputs hash"):
        validate_gold(gold, changed)
    gold["cases"][0]["case_id"] = "OTHER"
    with pytest.raises(ValueError, match="exact input case IDs"):
        validate_gold(gold, inputs)


def test_correct_run_scored_without_claiming_acceptance(task):
    gold, traces, human = evaluation(task)
    result = score_reports(traces, gold, human, "run-01")
    assert result["status"] == "scored"
    assert result["production_acceptance"] == "not_approved"
    assert result["metrics"]["research_fact_coverage"] == {"count": 2, "denominator": 2, "rate": 1.0}
    assert result["metrics"]["writer_numeric_accuracy"]["rate"] == 1.0
    assert result["metrics"]["report_usable_rate"]["rate"] == 1.0
    assert result["metrics"]["reviewer_detection_rate"] == {"count": 0, "denominator": 0, "rate": None}
    assert result["reviewer_observed_count"] == 1


@pytest.mark.parametrize("mutation,pattern", [
    (lambda h: h.update(status="pending_human_review"), "pending human"),
    (lambda h: h.update(reviewer=None), "reviewer"),
    (lambda h: h.update(reviewed_at="2026-10-24"), "timezone"),
    (lambda h: h["cases"][0]["fact_checks"][0].update(correct=None), "pending"),
    (lambda h: h["cases"][0]["fact_checks"][0].update(correct=False), "false requires a reason"),
    (lambda h: h["cases"][0]["numeric_checks"][0].update(correct_context=False), "false requires a reason"),
    (lambda h: h["cases"][0].update(critical_errors=None), "pending"),
    (lambda h: h["cases"][0].update(usable=False), "usable_note"),
    (lambda h: h["cases"][0]["fact_checks"].pop(), "exact gold IDs"),
])
def test_unreviewed_or_incomplete_judgments_block(task, mutation, pattern):
    gold, traces, human = evaluation(task)
    mutation(human)
    with pytest.raises(ValueError, match=pattern):
        score_reports(traces, gold, human, "run-01")


def test_semantic_fact_quote_cannot_come_from_topic_or_draft(task):
    trace = completed_trace()
    trace["outputs"]["research"]["topic"] = "正确但只出现在主题"
    gold, traces, human = evaluation(task, [trace])
    human["cases"][0]["fact_checks"][0]["output_quote"] = "正确但只出现在主题"
    with pytest.raises(ValueError, match="allowed output field"):
        score_reports(traces, gold, human, "run-01")


@pytest.mark.parametrize("change,pattern", [
    ({"output_quote": "不存在的数字18个百分点"}, "quote not found"),
    ({"observed_value": "17"}, "observed_value absent"),
    ({"observed_unit": "%"}, "observed_unit absent"),
])
def test_recorded_numbers_must_actually_appear_in_cited_draft(task, change, pattern):
    gold, traces, human = evaluation(task)
    human["cases"][0]["numeric_checks"][0].update(change)
    with pytest.raises(ValueError, match=pattern):
        score_reports(traces, gold, human, "run-01")


def test_wrong_number_or_unit_counts_wrong_after_human_context_confirmation(task):
    trace = completed_trace()
    trace["outputs"]["draft"]["content"] = "提高19个百分点，相对提高25元。"
    gold, traces, human = evaluation(task, [trace])
    case = human["cases"][0]
    case["numeric_checks"][0].update(observed_value="19", output_quote="提高19个百分点")
    case["numeric_checks"][1].update(observed_unit="元", output_quote="提高25元")
    case.update(usable=False, usable_note="数字和单位错误", critical_errors=[
        {"output_quote": "提高19个百分点", "evidence_note": "实际应为18个百分点"},
    ])
    case["reviewer_assessment"].update(draft_has_major_error=True, detected_major_error=False, evidence_note="Reviewer漏报")
    result = score_reports(traces, gold, human, "run-01")
    assert result["metrics"]["writer_numeric_accuracy"] == {"count": 0, "denominator": 2, "rate": 0.0}
    assert result["metrics"]["critical_report_rate"]["rate"] == 1.0
    assert result["metrics"]["reviewer_detection_rate"] == {"count": 0, "denominator": 1, "rate": 0.0}
    case["usable"] = True
    with pytest.raises(ValueError, match="usable=true conflicts"):
        score_reports(traces, gold, human, "run-01")


@pytest.mark.parametrize("major", [True, False])
def test_reviewer_positive_judgments_bind_real_issue_evidence(task, major):
    trace = completed_trace()
    trace["outputs"]["review"] = {
        "decision": "rewrite", "issues": [{"issue_id": "I001", "reason": "独立核真没有证据支持"}],
    }
    if major:
        trace["outputs"]["draft"]["content"] += "本结果已独立核真。"
    gold, traces, human = evaluation(task, [trace])
    case = human["cases"][0]
    if major:
        case.update(usable=False, usable_note="严重错误需修订", critical_errors=[
            {"output_quote": "本结果已独立核真", "evidence_note": "缓存材料未独立核真"},
        ])
    assessment = case["reviewer_assessment"]
    assessment.update(draft_has_major_error=major, detected_major_error=major,
                      false_alarm=not major, issue_ids=["I001"], output_quote="独立核真没有证据支持")
    result = score_reports(traces, gold, human, "run-01")
    metric = "reviewer_detection_rate" if major else "reviewer_false_alarm_rate"
    assert result["metrics"][metric] == {"count": 1, "denominator": 1, "rate": 1.0}
    assessment["issue_ids"] = ["I999"]
    with pytest.raises(ValueError, match="unknown issue_id"):
        score_reports(traces, gold, human, "run-01")
    assessment["issue_ids"] = ["I001"]
    assessment["output_quote"] = "没有出现在Reviewer中的主张"
    with pytest.raises(ValueError, match="quote not found"):
        score_reports(traces, gold, human, "run-01")


def test_approved_empty_reviewer_output_cannot_be_annotated_as_detection(task):
    trace = completed_trace()
    trace["outputs"]["draft"]["content"] += "本结果已独立核真。"
    gold, traces, human = evaluation(task, [trace])
    case = human["cases"][0]
    case.update(usable=False, usable_note="严重错误", critical_errors=[
        {"output_quote": "本结果已独立核真", "evidence_note": "无证据"},
    ])
    case["reviewer_assessment"].update(draft_has_major_error=True, detected_major_error=True,
                                        issue_ids=["I001"], output_quote="本结果已独立核真")
    with pytest.raises(ValueError, match="approve cannot support"):
        score_reports(traces, gold, human, "run-01")


@pytest.mark.parametrize("asset", ["gold", "trace", "run_id"])
def test_identity_drift_blocks_signed_review(task, asset):
    gold, traces, human = evaluation(task)
    run_id = "run-01"
    if asset == "gold":
        gold["cases"][0]["usable_definition"] += "被修改"
    elif asset == "trace":
        traces[0]["outputs"]["draft"]["content"] += "被修改"
    else:
        run_id = "another-run"
    with pytest.raises(ValueError, match="hash mismatch|run_id mismatch"):
        score_reports(traces, gold, human, run_id)


def failed_trace(stage="research", retained_invalid_output=False):
    trace = completed_trace()
    trace["status"] = "failed"
    failed = False
    mapping = {"research": "research", "outline": "outline", "assemble": "draft", "review": "review"}
    for record in trace["stages"]:
        if record["stage"] == stage:
            failed = True
            record["status"] = "failed"
        elif failed:
            record["status"] = "not_run"
        if failed and record["stage"] in mapping:
            if not (retained_invalid_output and record["stage"] == stage):
                trace["outputs"][mapping[record["stage"]]] = None
    return trace


def test_execution_failure_keeps_fixed_denominators_without_reviewing_absent_outputs(task):
    inputs, labels = task
    second_input, second_label = deepcopy(inputs[0]), deepcopy(labels[0])
    for row in (second_input, second_label):
        row.update(case_id="E02-REPORT", event_group_id="E02")
    gold = approved(make_gold_template(inputs + [second_input], labels + [second_label]))
    failed = failed_trace()
    failed.update(case_id="E02-REPORT", event_group_id="E02")
    traces = [completed_trace(), failed]
    human = make_human_review(traces, gold, "run-01")
    filled_first = fill_human({**deepcopy(human), "cases": [deepcopy(human["cases"][0])]})
    human["cases"][0] = filled_first["cases"][0]
    approved(human)  # Failure case decisions remain null; no nonexistent content is judged.
    result = score_reports(traces, gold, human, "run-01")
    assert (result["planned"], result["completed"], result["failed"]) == (2, 1, 1)
    assert result["metrics"]["research_fact_coverage"] == {"count": 2, "denominator": 4, "rate": 0.5}
    assert result["metrics"]["writer_numeric_accuracy"] == {"count": 2, "denominator": 4, "rate": 0.5}
    assert result["metrics"]["report_usable_rate"]["rate"] == 0.5
    assert result["metrics"]["critical_report_rate"]["denominator"] == 1


def test_failed_stage_parsed_output_is_diagnostic_not_successful_credit(task):
    gold, traces, human = evaluation(task, [failed_trace(retained_invalid_output=True)])
    # No semantic review required for invalid failed-stage output.
    human = approved(make_human_review(traces, gold, "run-01"))
    result = score_reports(traces, gold, human, "run-01")
    assert result["metrics"]["research_fact_coverage"]["count"] == 0
    assert result["metrics"]["report_usable_rate"]["count"] == 0


def test_upstream_generated_research_still_needs_human_review_after_later_failure(task):
    gold, traces, human = evaluation(task, [failed_trace(stage="outline")])
    human = approved(make_human_review(traces, gold, "run-01"))
    with pytest.raises(ValueError, match="pending"):
        score_reports(traces, gold, human, "run-01")
    for fact in human["cases"][0]["fact_checks"]:
        fact.update(correct=True, output_quote="基期72%，本期90%。")
    result = score_reports(traces, gold, human, "run-01")
    assert result["metrics"]["research_fact_coverage"]["rate"] == 1.0
    assert result["metrics"]["writer_numeric_accuracy"]["rate"] == 0.0


def test_failed_review_counts_missed_detection_but_not_no_false_alarm(task):
    trace = failed_trace(stage="review", retained_invalid_output=True)
    trace["outputs"]["draft"]["content"] += "此结论已独立核实。"
    gold, traces, human = evaluation(task, [trace])
    case = human["cases"][0]
    case.update(usable=False, usable_note="Reviewer阶段失败，且存在严重错误", critical_errors=[
        {"output_quote": "此结论已独立核实", "evidence_note": "来源仅为缓存转述，未独立核真"},
    ])
    case["reviewer_assessment"].update(draft_has_major_error=True, detected_major_error=None, false_alarm=None)
    result = score_reports(traces, gold, human, "run-01")
    assert result["metrics"]["reviewer_detection_rate"] == {"count": 0, "denominator": 1, "rate": 0.0}
    assert result["metrics"]["reviewer_false_alarm_rate"] == {"count": 0, "denominator": 0, "rate": None}
    assert result["reviewer_observed_count"] == 0
    assert result["metrics"]["report_usable_rate"]["rate"] == 0.0


@pytest.mark.parametrize("mutation,pattern", [
    (lambda traces: traces[0].update(status="blocked"), "blocked/not_run"),
    (lambda traces: traces[0].update(status="failed"), "actual failed stage evidence"),
    (lambda traces: traces[0]["outputs"].update(research=None), "missing output"),
    (lambda traces: traces.append(deepcopy(traces[0])), "duplicate"),
])
def test_missing_or_blocked_execution_is_never_relabelled_failed(task, mutation, pattern):
    gold, traces, human = evaluation(task)
    mutation(traces)
    # Rebind a new template to isolate coverage validation from tamper checks.
    if len(traces) > 1:
        with pytest.raises(ValueError, match=pattern):
            make_human_review(traces, gold, "run-01")
        return
    human = fill_human(make_human_review(traces, gold, "run-01"))
    with pytest.raises(ValueError, match=pattern):
        score_reports(traces, gold, human, "run-01")


def test_missing_prediction_and_per_case_hash_mismatch_block(task):
    gold, traces, human = evaluation(task)
    human["cases"][0]["trace_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="trace hash"):
        score_report_case(traces[0], gold["cases"][0], human["cases"][0])
    human = approved(make_human_review([], gold, "run-01"))
    with pytest.raises(ValueError, match="traces"):
        score_reports([], gold, human, "run-01")


def test_canonical_identity_ignores_dict_key_order_only():
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})
    assert canonical_hash([1, 2]) != canonical_hash([2, 1])
    with pytest.raises(ValueError):
        canonical_hash(float("nan"))
