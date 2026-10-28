"""Reproduce source/Decimal checks and aggregate explicitly provisional assistant labels."""
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[3]
RUN = Path(__file__).resolve().parent

def read(path):
    return json.loads(path.read_text(encoding="utf-8"))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def check():
    package = read(RUN/"editing-tasks.json")
    proposal = read(RUN/"assistant-review.proposal.json")
    gold_path = ROOT/"agent/evaluation/datasets/writing/report_pilot_v1/private/gold-review.json"
    assert proposal["status"] == "pending_human_review" and proposal["review_type"] == "assistant_proposal"
    assert sha(gold_path) == proposal["gold_sha256"] == package["gold_sha256"]
    gold = read(gold_path)
    cases = {c["case_id"]: c for c in gold["cases"]}
    inputs = {r["case_id"]:r for r in read(RUN/"inputs.json")}
    tasks = {t["blind_id"]:t for t in package["tasks"]}
    assert {r["blind_id"] for r in proposal["reviews"]} == set(tasks) and len(tasks)==4
    result = {arm:{"core_fact_coverage":[0,0],"required_numeric_pass":[0,0],
        "critical_report_rate":[0,0],"required_check_failure_report_rate":[0,0],"usable_report_rate":[0,0]}
        for arm in ("baseline","candidate")}
    details = []
    for review in proposal["reviews"]:
        task = tasks[review["blind_id"]]
        text = task["draft_content"]
        draft = RUN/"cases"/task["case_id"]/task["arm"]/"draft.md"
        assert sha(draft) == review["draft_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        g = cases[task["case_id"]]
        facts = {f["fact_id"]:f for f in g["source_facts"]}
        assert [r["fact_id"] for r in review["fact_checks"]] == g["required_fact_ids"]
        assert [r["check_id"] for r in review["numeric_checks"]] == [c["check_id"] for c in g["numeric_checks"]]
        correct_facts = 0
        for row in review["fact_checks"]:
            assert type(row["correct"]) is bool
            if row["correct"]:
                assert row["output_quote"] and row["output_quote"] in text
                correct_facts += 1
            else:
                assert row["evidence_note"]
        passed = 0
        checks = []
        for expected, observed in zip(g["numeric_checks"],review["numeric_checks"]):
            new, old = [Decimal(facts[k]["value"]) for k in expected["operands"]]
            calculated = new-old
            if expected["operation"] == "relative_change_percent":
                calculated = (new-old)/old*100
            assert abs(calculated-Decimal(expected["expected_value"])) <= Decimal(expected["tolerance"])
            valid = False
            if observed["correct_context"]:
                quote=observed["output_quote"]
                assert quote and quote in text
                value=Decimal(observed["observed_value"])
                numbers=[Decimal(x) for x in re.findall(r"(?<![\d.])[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?![\d.])",quote)]
                assert value in numbers and observed["observed_unit"] in quote
                valid = (observed["observed_unit"]==expected["unit"] and
                         abs(value-Decimal(expected["expected_value"]))<=Decimal(expected["tolerance"]))
            else:
                assert observed["evidence_note"]
            passed += int(valid)
            checks.append({"check_id":expected["check_id"],"passed":valid,"expected":expected["expected_value"],
                           "observed":observed["observed_value"]})
        for error in review["critical_errors"]:
            assert error["output_quote"] in text and error["evidence_note"]
            if error.get("source_quote"):
                doc=next(d for d in inputs[task["case_id"]]["input"]["requirements"]["source_documents"]
                         if d["source_id"]==error["source_id"])
                assert error["source_quote"] in doc["content"]
        metrics=result[task["arm"]]
        entries={"core_fact_coverage":(correct_facts,len(g["required_fact_ids"])),
            "required_numeric_pass":(passed,len(g["numeric_checks"])),
            "critical_report_rate":(int(bool(review["critical_errors"])),1),
            "required_check_failure_report_rate":(int(correct_facts<len(g["required_fact_ids"]) or passed<len(g["numeric_checks"])),1),
            "usable_report_rate":(int(review["usable"]),1)}
        for name,(n,d) in entries.items():
            metrics[name][0]+=n;metrics[name][1]+=d
        details.append({"blind_id":task["blind_id"],"case_id":task["case_id"],"arm":task["arm"],
                        "numeric_checks":checks,"correct_facts":correct_facts,"critical_errors":len(review["critical_errors"])})
    snapshots=read(RUN/"metric-snapshots.json")
    source_metrics=0
    derived_values=0
    for case,snapshot in snapshots.items():
        docs={d["source_id"]:d for d in inputs[case]["input"]["requirements"]["source_documents"]}
        candidate=(RUN/"cases"/case/"candidate/draft.md").read_text(encoding="utf-8")
        for metric in snapshot["metrics"]:
            evidence=metric["evidence"];body=docs[evidence["source_id"]]["content"]
            assert hashlib.sha256(body.encode()).hexdigest()==evidence["source_content_sha256"]
            quote=body[evidence["quote_start"]:evidence["quote_end"]]
            assert quote==evidence["quote"] and hashlib.sha256(quote.encode()).hexdigest()==evidence["quote_sha256"]
            assert body[evidence["match_start"]:evidence["match_end"]]==evidence["matched_text"]
            old,new=Decimal(metric["base_value"]),Decimal(metric["current_value"])
            assert Decimal(metric["absolute_change"]["value"])==new-old
            relative=((new-old)/old*100).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
            assert Decimal(metric["relative_change"]["value"])==relative
            assert metric["absolute_change"]["display"] in candidate and metric["relative_change"]["display"] in candidate
            assert evidence["source_url"] in candidate
            source_metrics+=1;derived_values+=2
    ratios={arm:{name:{"count":v[0],"denominator":v[1],"rate":v[0]/v[1] if v[1] else None}
                 for name,v in metrics.items()} for arm,metrics in result.items()}
    outcome={"status":"assistant_review_provisional","human_approved":False,
        "scope":"two development report tasks; local model; direct writing with Python metric appendix; not native RWR",
        "events":2,"tasks":2,"paired_drafts":4,"formal_independent_test":False,
        "metrics":ratios,"source_metric_evidence_verified":source_metrics,
        "derived_numeric_values_recomputed":derived_values,"per_draft":details,
        "human_editing_minutes":None,"valid_as_human_reviewed_resume_metrics":False,
        "limitations":["AI semantic proposals still require user confirmation",
            "source table correctness does not prove narrative or final report usability",
            "candidate contains a deterministic computed appendix; this is not a Prompt-only effect",
            "two development cases; revisions were designed after observing development failures",
            "source citations and one required cash-flow fact still need human editing",
            "real manual editing time is collected separately, never inferred from model elapsed time"],
        "evidence_sha256":{str(p.relative_to(ROOT)):sha(p) for p in
            (gold_path,RUN/"assistant-review.proposal.json",RUN/"editing-tasks.json",Path(__file__).resolve())}}
    with (RUN/"provisional-metrics.json").open("x",encoding="utf-8") as f:
        json.dump(outcome,f,ensure_ascii=False,indent=2);f.write("\n")
    print(json.dumps({k:v for k,v in outcome.items() if k not in ("per_draft","evidence_sha256")},ensure_ascii=False))

if __name__=="__main__":
    check()
