"""Offline paired human editing records, never estimated from model latency.

From agent/: python -m evaluation.editing_time template --help / score --help.
Templates never approve labels or fill human work. Hashes/times/signatures are
declared metadata, not independent proof of labour or artifact correctness.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

ARMS = ("baseline", "candidate")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value, name):
    _require(isinstance(value, str) and bool(value.strip()), f"{name}: nonempty text required")
    return value


def _date(value, name):
    try:
        result = datetime.fromisoformat(_text(value, name).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name}: ISO timestamp with timezone required") from None
    _require(result.tzinfo is not None and result.utcoffset() is not None,
             f"{name}: explicit timezone required")
    return result


def _hash(value, name):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None,
             f"{name}: 64 hexadecimal SHA256 characters required")
    return value.lower()


def _plan(rows):
    _require(isinstance(rows, list) and bool(rows), "planned_cases: nonempty list required")
    result = {}
    for row in rows:
        _require(isinstance(row, dict), "planned_cases: objects required")
        case_id = _text(row.get("case_id"), "planned case_id")
        _require(case_id not in result, "planned_cases: duplicate case_id")
        result[case_id] = _text(row.get("event_group_id"), "planned event_group_id")
    return result


def make_template(gold, *, experiment_id, criterion_version, criterion_text, gold_sha256):
    """Use gold IDs only; pending gold may create an unapproved form."""
    _require(isinstance(gold, dict), "gold: object required")
    cases = _plan(gold.get("cases"))
    _hash(gold_sha256, "gold_sha256")
    for value, name in ((experiment_id, "experiment_id"), (criterion_version, "criterion_version"),
                        (criterion_text, "criterion_text")):
        _text(value, name)
    records = []
    for case_id, group in cases.items():
        for arm in ARMS:
            records.append({
                "case_id": case_id, "event_group_id": group, "arm": arm,
                "criterion_version": criterion_version, "editor": None, "source_run_id": None,
                "draft_sha256": None, "edited_sha256": None, "blind_id": None, "order_index": None,
                "started_at": None, "finished_at": None, "paused_seconds": None,
                "reviewer": None, "reviewed_at": None, "meets_criterion": None, "notes": "",
            })
    return {
        "schema_version": 1, "experiment_id": experiment_id, "status": "pending_human_review",
        "reviewer": None, "reviewed_at": None, "data_origin": None, "gold_sha256": gold_sha256.lower(),
        "gold_status_at_template": gold.get("status"), "criterion_version": criterion_version,
        "criterion_text": criterion_text,
        "planned_cases": [{"case_id": key, "event_group_id": group} for key, group in cases.items()],
        "records": records,
        "instructions": [
            "Fill genuine editing times, never infer minutes from model latency.",
            "Use the same completion criterion; active reading, evidence checking and correction count.",
            "paused_seconds excludes interruptions/model waiting; overlapping pauses count only once.",
            "Record blind IDs and each editor's positive order_index; balance arm order where possible.",
            "Set data_origin to human_recorded, or synthetic_test for artificial fixtures.",
            "A human signs status=approved after checking all records; this is not production approval.",
        ],
    }


def score_records(pack):
    """Reject incomplete pre-registered pairs; missing values never become zero."""
    _require(isinstance(pack, dict) and type(pack.get("schema_version")) is int
             and pack["schema_version"] == 1, "unsupported records pack")
    _require(pack.get("status") == "approved", "records require explicit human approval")
    experiment_id = _text(pack.get("experiment_id"), "experiment_id")
    _text(pack.get("reviewer"), "pack reviewer")
    approved_at = _date(pack.get("reviewed_at"), "pack reviewed_at")
    origin = pack.get("data_origin")
    _require(origin in ("human_recorded", "synthetic_test"), "data_origin must identify human or synthetic records")
    criterion = _text(pack.get("criterion_version"), "criterion_version")
    _text(pack.get("criterion_text"), "criterion_text")
    gold_hash = _hash(pack.get("gold_sha256"), "gold_sha256")
    cases = _plan(pack.get("planned_cases"))
    rows = pack.get("records")
    _require(isinstance(rows, list) and len(rows) == len(cases) * 2,
             "records: exactly baseline and candidate for every planned case required")
    records, editor_orders = {}, set()
    for row in rows:
        _require(isinstance(row, dict), "records: objects required")
        case_id = _text(row.get("case_id"), "record case_id")
        _require(case_id in cases, "record case_id not planned")
        _require(row.get("event_group_id") == cases[case_id], "record event_group_id disagrees with plan")
        arm = row.get("arm")
        _require(arm in ARMS, "record arm must be baseline or candidate")
        key = case_id, arm
        _require(key not in records, "records: duplicate case/arm")
        _require(row.get("criterion_version") == criterion, "record completion criterion differs")
        editor = _text(row.get("editor"), "record editor")
        _text(row.get("source_run_id"), "record source_run_id")
        _hash(row.get("draft_sha256"), "record draft_sha256")
        _hash(row.get("edited_sha256"), "record edited_sha256")
        _text(row.get("blind_id"), "record blind_id")
        order = row.get("order_index")
        _require(type(order) is int and order > 0, "record order_index: positive integer required")
        _require((editor, order) not in editor_orders, "duplicate editor/order_index")
        editor_orders.add((editor, order))
        _text(row.get("reviewer"), "record reviewer")
        reviewed_at = _date(row.get("reviewed_at"), "record reviewed_at")
        _require(row.get("meets_criterion") is True, "every planned edit must meet the same completion criterion")
        start, finish = (_date(row.get(field), "record " + field) for field in ("started_at", "finished_at"))
        elapsed = (finish - start).total_seconds()
        _require(math.isfinite(elapsed) and elapsed >= 0, "elapsed time must be finite and nonnegative")
        paused = row.get("paused_seconds")
        _require(type(paused) in (int, float) and math.isfinite(paused) and 0 <= paused <= elapsed,
                 "paused_seconds must be finite and within elapsed time")
        _require(finish <= reviewed_at <= approved_at, "reviews must follow completion and precede pack approval")
        records[key] = elapsed - paused
    _require(set(records) == {(key, arm) for key in cases for arm in ARMS}, "records do not match planned pairs")
    pairs = []
    for case_id, group in cases.items():
        before, after = (records[case_id, arm] for arm in ARMS)
        pairs.append({"case_id": case_id, "event_group_id": group,
                      "baseline_active_seconds": before, "candidate_active_seconds": after,
                      "reduction_minutes": (before - after) / 60})
    stats = {}
    for arm in ARMS:
        seconds = [records[key, arm] for key in cases]
        stats[arm] = {"count": len(seconds), "total_active_seconds": sum(seconds),
                      "mean_numerator_minutes": sum(seconds) / 60, "mean_denominator_records": len(seconds),
                      "mean_minutes": statistics.mean(seconds) / 60,
                      "median_minutes": statistics.median(seconds) / 60}
    before, after = (stats[arm]["total_active_seconds"] for arm in ARMS)
    reductions = [pair["reduction_minutes"] for pair in pairs]
    return {
        "schema_version": 1, "status": "scored", "experiment_id": experiment_id, "data_origin": origin,
        "criterion_version": criterion, "gold_sha256": gold_hash, "planned_case_count": len(cases),
        "paired_case_count": len(pairs), "planned_record_count": len(cases) * 2,
        "scored_record_count": len(records), "arms": stats,
        "paired": {
            "count": len(pairs), "mean_reduction_minutes": statistics.mean(reductions),
            "median_reduction_minutes": statistics.median(reductions),
            "reduction_numerator_minutes": sum(reductions), "reduction_denominator_pairs": len(pairs),
            "relative_reduction_numerator_seconds": before - after,
            "relative_reduction_denominator_seconds": before,
            "relative_reduction_percent": (before - after) / before * 100 if before else None,
            "improved_pairs": sum(value > 0 for value in reductions),
            "unchanged_pairs": sum(value == 0 for value in reductions),
            "worsened_pairs": sum(value < 0 for value in reductions),
        },
        "pairs": pairs, "production_acceptance": "not_evaluated",
        "conclusion_scope": "descriptive paired manual editing times only",
        "limitations": [
            "Human-entered times, signatures and hashes do not independently prove genuine labour.",
            "Hashes identify declared artifacts; this tool does not read model runs or verify draft content.",
            "Entered criterion approval is not an independent check of final text quality.",
            "Editor skill, order and prior exposure affect times; blind IDs do not prove actual blinding.",
            "No confidence interval, causal improvement, production acceptance or enterprise benefit inferred.",
            "Incomplete plans are rejected, never silently dropped; report their status separately.",
        ],
    }


def _write_new(path, value):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(encoded)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    form = sub.add_parser("template", help="Create unapproved planned pairs; no timer or model.")
    form.add_argument("--gold-file", type=Path, required=True)
    form.add_argument("--experiment-id", required=True)
    form.add_argument("--criterion-version", required=True)
    form.add_argument("--criterion-text", required=True)
    form.add_argument("--output", type=Path, required=True)
    score = sub.add_parser("score", help="Validate approved records and aggregate paired descriptive times.")
    score.add_argument("--records", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        source = args.gold_file if args.command == "template" else args.records
        raw = source.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
        source_hash = hashlib.sha256(raw).hexdigest()
        if args.command == "template":
            value = make_template(parsed, experiment_id=args.experiment_id,
                                  criterion_version=args.criterion_version, criterion_text=args.criterion_text,
                                  gold_sha256=source_hash)
        else:
            value = score_records(parsed)
            value["records_file_sha256"] = source_hash
        _write_new(args.output, value)
    except (ValueError, OSError, TypeError, KeyError, OverflowError) as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, ValueError) else
                          "Check files/field types; existing outputs are never overwritten."}, ensure_ascii=False))
        raise SystemExit(2) from None
    print(json.dumps({"status": value["status"], "output": str(args.output.resolve()),
                      "paired_case_count": value.get("paired_case_count"),
                      "production_acceptance": "not_evaluated"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
