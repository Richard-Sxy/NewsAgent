"""Offline report scoring: human semantics, Decimal arithmetic, immutable identities.

No model, network, application settings, keyword-based semantic judge or automatic
approval is used here. Call validate_gold against the frozen inputs before a run.
Natural-draft Reviewer observations are not an injected-error probe benchmark.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from urllib.parse import urlsplit


METRICS = (
    "research_fact_coverage", "writer_numeric_accuracy", "critical_report_rate",
    "report_usable_rate", "reviewer_detection_rate", "reviewer_false_alarm_rate",
)
CRITICAL_DEFINITION = (
    "建议：捏造或误述核心事实、金额/单位/期间/同比环比口径错误、把亏损写成利润、"
    "把缓存转述写成已独立核实的事实，或违反本例禁止断言并实质改变结论，属于严重错误。"
    "请审核者结合具体样本修改并确认。"
)
USABLE_DEFINITION = (
    "建议：研究覆盖全部必需事实，成稿数字、单位和比较口径正确，保留来源归属与必需限制，"
    "没有严重错误，结构与任务要求相符，可供运营编辑使用；未完成流程不算可用报告。"
    "请审核者结合具体样本修改并确认。"
)


def canonical_hash(value: object) -> str:
    """Hash JSON content rather than editable indentation or object key order."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _text(value: object, name: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{name}: nonempty text required")
    return value


def _decimal(value: object, name: str) -> Decimal:
    _require(type(value) in (str, int, float), f"{name}: finite decimal required")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name}: finite decimal required") from None
    _require(number.is_finite(), f"{name}: finite decimal required")
    return number


def _rows(rows: object, key: str, name: str, *, allow_empty: bool = False) -> dict:
    _require(isinstance(rows, list) and (allow_empty or bool(rows)), f"{name}: list required")
    result = {}
    for row in rows:
        _require(isinstance(row, dict), f"{name}: object required")
        identifier = _text(row.get(key), f"{name}.{key}")
        _require(identifier not in result, f"{name}: duplicate {key} {identifier}")
        result[identifier] = row
    return result


def _strings(values: object, name: str, *, nonempty: bool = False) -> list:
    _require(isinstance(values, list) and (not nonempty or bool(values)), f"{name}: list required")
    for value in values:
        _text(value, name)
    _require(len(values) == len(set(values)), f"{name}: duplicate value")
    return values


def _approved(pack: dict, name: str, required: bool = True) -> None:
    _require(pack.get("schema_version") == 1, f"{name}: unsupported schema_version")
    _require(pack.get("status") in ("pending_human_review", "approved"), f"{name}: invalid status")
    if required:
        _require(pack["status"] == "approved", f"{name}: pending human review")
    if pack["status"] == "approved":
        _text(pack.get("reviewer"), f"{name}.reviewer")
        value = _text(pack.get("reviewed_at"), f"{name}.reviewed_at")
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{name}.reviewed_at: ISO timestamp required") from None
        _require(date.tzinfo is not None, f"{name}.reviewed_at: timezone required")


def _recompute(check: dict, facts: dict) -> Decimal:
    operands = _strings(check.get("operands"), "numeric operands", nonempty=True)
    _require(len(operands) == 2 and all(key in facts for key in operands),
             "numeric operands: exactly two known fact IDs required")
    new, old = (facts[key] for key in operands)
    a, b = (_decimal(fact.get("value"), "operand value") for fact in (new, old))
    _require(new.get("unit") == old.get("unit"), "numeric operands: mismatched units")
    operation = check.get("operation")
    with localcontext() as context:
        context.prec = 40
        if operation == "delta":
            _require(check.get("unit") == new.get("unit"), "delta: output unit mismatch")
            return a - b
        if operation == "relative_change_percent":
            _require(b != 0, "relative change: zero baseline")
            _require(check.get("unit") == "%", "relative change: expected % unit")
            return (a - b) / b * Decimal(100)
        if operation in ("percentage_point_delta", "basis_point_delta"):
            _require(new.get("unit") == "%", "percentage operands must use percentage-valued % units")
            expected_unit = "个百分点" if operation == "percentage_point_delta" else "基点"
            _require(check.get("unit") == expected_unit, "percentage delta: output unit mismatch")
            return (a - b) * (Decimal(1) if operation == "percentage_point_delta" else Decimal(100))
    raise ValueError(f"unsupported numeric operation: {operation}")


def _validate_case_gold(case: dict, input_row: dict | None = None) -> None:
    for key in ("case_id", "event_group_id", "split", "critical_error_definition", "usable_definition"):
        _text(case.get(key), f"gold case.{key}")
    facts = _rows(case.get("source_facts"), "fact_id", "source_facts")
    source_documents = None
    if input_row is not None:
        for key in ("case_id", "event_group_id", "split"):
            _require(case[key] == input_row.get(key), f"gold case {key}: input identity mismatch")
        try:
            documents = input_row["input"]["requirements"]["source_documents"]
        except (KeyError, TypeError):
            raise ValueError("inputs: missing source_documents") from None
        source_documents = _rows(documents, "source_id", "source_documents")
    for fact in facts.values():
        _text(fact.get("statement"), "fact.statement")
        _text(fact.get("source_id"), "fact.source_id")
        url = urlsplit(_text(fact.get("source_url"), "fact.source_url"))
        _require(url.scheme in ("http", "https") and bool(url.netloc), "fact.source_url: invalid URL")
        locator = _text(fact.get("locator"), "fact.locator")
        match = re.fullmatch(r"content\.chars\[(\d+):(\d+)\]", locator)
        _require(match is not None, "fact.locator: expected content.chars[start:end]")
        start, end = map(int, match.groups())
        _require(0 <= start < end, "fact.locator: empty or reversed range")
        if "value" in fact or "unit" in fact:
            _decimal(fact.get("value"), "fact.value")
            _text(fact.get("unit"), "fact.unit")
        if source_documents is not None:
            _require(fact["source_id"] in source_documents, "fact: unknown source_id")
            source = source_documents[fact["source_id"]]
            _require(fact["source_url"] == source.get("url"), "fact: source URL mismatch")
            content = _text(source.get("content"), "source.content")
            _require(end <= len(content), "fact.locator: out of source bounds")
    required = _strings(case.get("required_fact_ids"), "required_fact_ids", nonempty=True)
    _require(set(required) <= set(facts), "required_fact_ids: unknown fact")
    for field in ("forbidden_claims", "required_caveats"):
        _strings(case.get(field), field)
    checks = _rows(case.get("numeric_checks"), "check_id", "numeric_checks", allow_empty=True)
    for check in checks.values():
        expected = _decimal(check.get("expected_value"), "expected_value")
        tolerance = _decimal(check.get("tolerance"), "tolerance")
        _require(tolerance >= 0, "tolerance: must be nonnegative")
        _text(check.get("unit"), "numeric unit")
        computed = _recompute(check, facts)
        _require(abs(computed - expected) <= tolerance, "numeric expected_value disagrees with Decimal recomputation")


def make_gold_template(input_rows: list, draft_labels: list) -> dict:
    """Promote only report-task expectations to an editable, unapproved template."""
    inputs = _rows(input_rows, "case_id", "inputs")
    labels = _rows(draft_labels, "case_id", "draft_labels")
    _require(set(inputs) <= set(labels), "draft_labels: missing case")
    cases = []
    for case_id, row in inputs.items():
        label = labels[case_id]
        sources = _rows(row["input"]["requirements"]["source_documents"], "source_id", "source_documents")
        facts = []
        for source in label["source_fact_gold"]:
            _require(source["source_id"] in sources, "draft fact: unknown source_id")
            for original in source["facts"]:
                fact = {key: deepcopy(original[key]) for key in ("fact_id", "statement", "value", "unit") if key in original}
                fact.update(source_id=source["source_id"], source_url=sources[source["source_id"]]["url"],
                            locator=original["evidence_locator"])
                facts.append(fact)
        expected = [label["stage_labels"][stage]["expected"] for stage in ("research", "write")]
        case = {key: row[key] for key in ("case_id", "event_group_id", "split")}
        case.update(source_facts=facts, numeric_checks=deepcopy(expected[1]["numeric_checks"]),
                    critical_error_definition=CRITICAL_DEFINITION, usable_definition=USABLE_DEFINITION)
        for key in ("required_fact_ids", "forbidden_claims", "required_caveats"):
            case[key] = list(dict.fromkeys(value for stage in expected for value in stage[key]))
        cases.append(case)
    gold = {"schema_version": 1, "status": "pending_human_review", "reviewer": None, "reviewed_at": None,
            "inputs_sha256": canonical_hash(input_rows), "cases": cases}
    validate_gold(gold, input_rows, require_approved=False)
    return gold


def validate_gold(gold: dict, input_rows: list, *, require_approved: bool = True) -> None:
    _require(isinstance(gold, dict), "gold: object required")
    _approved(gold, "gold", require_approved)
    inputs = _rows(input_rows, "case_id", "inputs")
    cases = _rows(gold.get("cases"), "case_id", "gold.cases")
    _require(list(inputs) == list(cases), "gold: exact input case IDs and order required")
    _require(gold.get("inputs_sha256") == canonical_hash(input_rows), "gold: inputs hash mismatch")
    for case_id, case in cases.items():
        _validate_case_gold(case, inputs[case_id])


def make_human_review(traces: list, gold: dict, run_id: str) -> dict:
    """Freeze trace identities; leave every semantic decision for a human."""
    _text(run_id, "run_id")
    gold_cases = _rows(gold.get("cases"), "case_id", "gold.cases")
    trace_cases = _rows(traces, "case_id", "traces", allow_empty=True)
    _require(set(trace_cases) <= set(gold_cases), "traces: unknown case")
    cases = []
    for case_id, case in gold_cases.items():
        trace = trace_cases.get(case_id)
        cases.append({
            "case_id": case_id, "trace_sha256": canonical_hash(trace) if trace is not None else None,
            "fact_checks": [{"fact_id": key, "correct": None, "output_quote": "", "evidence_note": ""}
                            for key in case["required_fact_ids"]],
            "numeric_checks": [{"check_id": check["check_id"], "correct_context": None,
                                "observed_value": None, "observed_unit": None,
                                "output_quote": "", "evidence_note": ""} for check in case["numeric_checks"]],
            "critical_errors": None, "usable": None, "usable_note": "",
            "reviewer_assessment": {"draft_has_major_error": None, "detected_major_error": None,
                                    "false_alarm": None, "issue_ids": [], "output_quote": "",
                                    "evidence_note": ""},
        })
    return {"schema_version": 1, "run_id": run_id, "status": "pending_human_review",
            "reviewer": None, "reviewed_at": None, "gold_sha256": canonical_hash(gold),
            "traces_sha256": canonical_hash(traces), "cases": cases}


def _ratio(count: int, denominator: int) -> dict:
    return {"count": count, "denominator": denominator, "rate": count / denominator if denominator else None}


def _boolean(value: object, name: str) -> bool:
    _require(type(value) is bool, f"{name}: pending or invalid human judgment")
    return value


def _quote(value: object, texts: list[str], name: str) -> str:
    quote = _text(value, name)
    _require(any(quote in text for text in texts), f"{name}: quote not found in allowed output field")
    return quote


def _research_texts(research: dict) -> list[str]:
    # Gold fact IDs differ from runtime F001 IDs: humans map claims using quotes.
    facts = research.get("facts", [])
    _require(isinstance(facts, list) and all(isinstance(fact, dict) for fact in facts), "research: invalid facts")
    return [fact[key] for fact in facts for key in ("claim", "evidence")
            if isinstance(fact.get(key), str)]


def _trace_outputs(trace: dict, case: dict) -> dict:
    for key in ("case_id", "event_group_id", "split"):
        _require(trace.get(key) == case[key], f"trace: {key} identity mismatch")
    status = trace.get("status")
    _require(status in ("completed", "failed"), "trace: blocked/not_run execution cannot be scored")
    stages = trace.get("stages")
    _require(isinstance(stages, list), "trace: missing stages")
    stage_map = _rows(stages, "stage", "trace.stages")
    if status == "failed":
        _require(any(isinstance(stage, dict) and stage.get("status") == "failed" for stage in stages),
                 "trace: failed requires actual failed stage evidence, not a missing file")
    outputs = trace.get("outputs")
    _require(isinstance(outputs, dict), "trace: missing outputs")
    for key in ("research", "outline", "draft", "review"):
        _require(key in outputs and (outputs[key] is None or isinstance(outputs[key], dict)), f"outputs.{key}: missing or invalid")
    _require(isinstance(outputs.get("sections"), list), "outputs.sections: missing or invalid")
    valid = dict(outputs)
    for key, stage_name in (("research", "research"), ("outline", "outline"),
                            ("draft", "assemble"), ("review", "review")):
        stage = stage_map.get(stage_name)
        _require(stage is not None, f"trace: missing {stage_name} stage record")
        if stage.get("status") == "completed":
            _require(outputs[key] is not None, f"trace: completed {stage_name} stage has missing output")
        else:
            # A parsed value may have failed business/citation checks. It stays
            # in the trace for diagnosis, but cannot earn successful-stage credit.
            valid[key] = None
    if status == "completed":
        _require(all(valid[key] is not None for key in ("research", "outline", "draft", "review")),
                 "trace: completed execution has missing output")
    _require(valid["draft"] is None or valid["research"] is not None, "trace: draft lacks upstream research")
    _require(valid["review"] is None or valid["draft"] is not None, "trace: review lacks draft")
    return valid


def score_report_case(trace: dict, gold_case: dict, human_review_case: dict) -> dict:
    """Score one already approved review; score_reports enforces top-level signatures."""
    _validate_case_gold(gold_case)
    outputs = _trace_outputs(trace, gold_case)
    human = human_review_case
    _require(human.get("case_id") == trace["case_id"], "human case: identity mismatch")
    _require(human.get("trace_sha256") == canonical_hash(trace), "human case: trace hash mismatch")
    fact_checks = _rows(human.get("fact_checks"), "fact_id", "human.fact_checks")
    numeric_checks = _rows(human.get("numeric_checks"), "check_id", "human.numeric_checks", allow_empty=True)
    required_facts = gold_case["required_fact_ids"]
    required_numeric = gold_case["numeric_checks"]
    _require(list(fact_checks) == required_facts, "human fact_checks: exact gold IDs and order required")
    _require(list(numeric_checks) == [item["check_id"] for item in required_numeric], "human numeric_checks: exact gold IDs and order required")
    fact_results, numeric_results = [], []
    research = outputs["research"]
    for fact_id in required_facts:
        passed = False
        if research is not None:
            check = fact_checks[fact_id]
            passed = _boolean(check.get("correct"), f"{fact_id}.correct")
            if passed:
                _quote(check.get("output_quote"), _research_texts(research), f"{fact_id}.output_quote")
            else:
                _text(check.get("evidence_note"), f"{fact_id}: false requires a reason")
        fact_results.append({"fact_id": fact_id, "correct": passed,
                             "basis": "human_review" if research is not None else "execution_failed_before_research"})
    draft = outputs["draft"]
    content = _text(draft.get("content"), "draft.content") if draft is not None else None
    for expected in required_numeric:
        check_id = expected["check_id"]
        passed = False
        if draft is not None:
            check = numeric_checks[check_id]
            context_correct = _boolean(check.get("correct_context"), f"{check_id}.correct_context")
            if context_correct:
                quote = _quote(check.get("output_quote"), [content], f"{check_id}.output_quote")
                observed = _decimal(check.get("observed_value"), f"{check_id}.observed_value")
                observed_unit = _text(check.get("observed_unit"), f"{check_id}.observed_unit")
                # Confirm the recorded Arabic numeral occurs in the cited passage;
                # context and source semantics remain the human's responsibility.
                normalized = quote.replace(",", "").replace("−", "-").replace("％", "%")
                numbers = [Decimal(value) for value in re.findall(r"(?<![\d.])[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?![\d.])", normalized)]
                _require(observed in numbers, f"{check_id}: observed_value absent from output_quote")
                _require(observed_unit in normalized, f"{check_id}: observed_unit absent from output_quote")
                passed = (observed_unit == expected["unit"] and
                          abs(observed - _decimal(expected["expected_value"], "expected_value"))
                          <= _decimal(expected["tolerance"], "tolerance"))
            else:
                _text(check.get("evidence_note"), f"{check_id}: false requires a reason")
        numeric_results.append({"check_id": check_id, "correct": passed,
                                "basis": "human_context_and_decimal" if draft is not None else "execution_failed_before_draft"})
    critical, usable, major_error, detected, false_alarm = [], False, False, False, False
    if draft is not None:
        critical = human.get("critical_errors")
        _require(isinstance(critical, list), "critical_errors: pending human judgment")
        for error in critical:
            _require(isinstance(error, dict), "critical_errors: objects with output_quote/evidence_note required")
            _quote(error.get("output_quote"), [content], "critical_error.output_quote")
            _text(error.get("evidence_note"), "critical_error.evidence_note")
        usable = _boolean(human.get("usable"), "usable")
        if usable:
            _require(trace["status"] == "completed" and not critical
                     and all(item["correct"] for item in fact_results + numeric_results),
                     "usable=true conflicts with failures, incorrect required facts/numbers or critical errors")
        else:
            _text(human.get("usable_note"), "usable=false requires usable_note")
        assessment = human.get("reviewer_assessment")
        _require(isinstance(assessment, dict), "reviewer_assessment: required")
        major_error = _boolean(assessment.get("draft_has_major_error"), "draft_has_major_error")
        _require(major_error == bool(critical), "draft_has_major_error must match documented critical_errors")
        if outputs["review"] is not None:
            detected = _boolean(assessment.get("detected_major_error"), "detected_major_error")
            false_alarm = _boolean(assessment.get("false_alarm"), "false_alarm")
            _text(assessment.get("evidence_note"), "reviewer_assessment.evidence_note")
            _require(not detected or major_error, "detected_major_error=true requires a real major error")
            if detected or false_alarm:
                review = outputs["review"]
                _require(review.get("decision") != "approve", "Reviewer approve cannot support detection or false alarm")
                issues = _rows(review.get("issues"), "issue_id", "review.issues")
                issue_ids = _strings(assessment.get("issue_ids"), "reviewer_assessment.issue_ids", nonempty=True)
                _require(set(issue_ids) <= set(issues), "reviewer_assessment: unknown issue_id")
                reasons = [issues[key]["reason"] for key in issue_ids if isinstance(issues[key].get("reason"), str)]
                _quote(assessment.get("output_quote"), reasons, "reviewer_assessment.output_quote")
        else:
            _require(assessment.get("detected_major_error") in (None, False)
                     and assessment.get("false_alarm") in (None, False), "missing review cannot detect or raise an alarm")
    has_draft = draft is not None
    observed_review = outputs["review"] is not None
    metrics = {
        "research_fact_coverage": _ratio(sum(item["correct"] for item in fact_results), len(required_facts)),
        "writer_numeric_accuracy": _ratio(sum(item["correct"] for item in numeric_results), len(required_numeric)),
        "critical_report_rate": _ratio(int(bool(critical)), int(has_draft)),
        "report_usable_rate": _ratio(int(usable), 1),
        "reviewer_detection_rate": _ratio(int(major_error and detected), int(major_error)),
        "reviewer_false_alarm_rate": _ratio(int(false_alarm and not major_error), int(observed_review and not major_error)),
    }
    return {"case_id": trace["case_id"], "status": "scored", "execution_status": trace["status"],
            "metrics": metrics, "fact_results": fact_results, "numeric_results": numeric_results,
            "generated_draft": has_draft, "reviewer_observed": observed_review,
            "production_acceptance": "not_approved"}


def score_reports(traces: list, gold: dict, human: dict, run_id: str) -> dict:
    """Aggregate only complete, signed reviews; missing/pending artifacts block."""
    _approved(gold, "gold")
    _approved(human, "human")
    _text(run_id, "run_id")
    _require(human.get("run_id") == run_id, "human: run_id mismatch")
    _require(human.get("gold_sha256") == canonical_hash(gold), "human: gold hash mismatch")
    _require(human.get("traces_sha256") == canonical_hash(traces), "human: traces hash mismatch")
    _require(isinstance(gold.get("inputs_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", gold["inputs_sha256"]) is not None,
             "gold: missing input identity; validate against frozen inputs before scoring")
    cases = _rows(gold.get("cases"), "case_id", "gold.cases")
    trace_cases = _rows(traces, "case_id", "traces")
    human_cases = _rows(human.get("cases"), "case_id", "human.cases")
    _require(list(trace_cases) == list(cases), "traces: exact planned IDs and order required; missing predictions are not execution failures")
    _require(list(human_cases) == list(cases), "human: exact planned IDs and order required")
    results = [score_report_case(trace_cases[key], case, human_cases[key]) for key, case in cases.items()]
    metrics = {key: _ratio(sum(result["metrics"][key]["count"] for result in results),
                            sum(result["metrics"][key]["denominator"] for result in results)) for key in METRICS}
    return {
        "schema_version": 1, "run_id": run_id, "status": "scored",
        "planned": len(cases), "completed": sum(trace["status"] == "completed" for trace in traces),
        "failed": sum(trace["status"] == "failed" for trace in traces), "blocked": 0, "not_run": 0,
        "metrics": metrics, "cases": results,
        "generated_draft_count": sum(result["generated_draft"] for result in results),
        "reviewer_observed_count": sum(result["reviewer_observed"] for result in results),
        "inputs_sha256": gold["inputs_sha256"], "gold_sha256": canonical_hash(gold),
        "traces_sha256": canonical_hash(traces), "human_review_sha256": canonical_hash(human),
        "production_acceptance": "not_approved",
        "scope": "Human-reviewed cached-source first-pass reports; not independent source verification or injected-error probes. Parsed outputs from failed business validation remain diagnostic only.",
        "denominator_policy": {
            "research_fact_coverage": "All required facts in all planned cases; real execution failure before research contributes zero.",
            "writer_numeric_accuracy": "All required numeric checks in all planned cases; real execution failure before draft contributes zero.",
            "critical_report_rate": "Generated drafts only; absence of a draft is not evidence of an error-free report.",
            "report_usable_rate": "All planned reports; incomplete or failed execution is never usable.",
            "reviewer_detection_rate": "Human-confirmed major-error drafts; failed review without output contributes zero detection.",
            "reviewer_false_alarm_rate": "Human-confirmed error-free drafts with an actual Reviewer output; absent reviews are not negative predictions.",
        },
    }
