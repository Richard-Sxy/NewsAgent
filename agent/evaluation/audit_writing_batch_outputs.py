"""Offline format and explicit-source-traceability signals for a frozen 100-task run.

This module does not call a model, grade factual correctness, approve gold,
measure human editing time, or construct a quality/release gate. The algorithm
must be frozen before examining evaluation drafts; do not tune it to outcomes.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from evaluation.run_writing_batch import ARMS, case_dir, status, verify, write_new

ALGORITHM_VERSION = "writing-output-signals-v1"
S_ID_PATTERN = r"(?<![\w-])S(?:[0-9]+|[-_][A-Za-z0-9][A-Za-z0-9_-]*)(?![\w-])"
URL_PATTERN = r"""https?://[^\s<>"'\x60\[\](){}\u3000\uff0c\u3002\uff1b\uff1a\uff01\uff1f\u3001]+"""
REF_PATTERN = r"\(@ref[^)]*\)"
MARKDOWN_SYMBOLS = "#*_~" + chr(96) + "><[](){}|!\\"
URL_RE = re.compile(URL_PATTERN)
S_ID_RE = re.compile(S_ID_PATTERN)
REF_RE = re.compile(REF_PATTERN)
ALGORITHM = {
    "version": ALGORITHM_VERSION,
    "scope": "format_and_explicit_source_traceability_signals_only",
    "planned_tasks": 100,
    "planned_requests": 200,
    "planned_per_arm": 100,
    "length_min_inclusive": 250,
    "length_max_inclusive": 350,
    "length_unit": "Python len Unicode code points; no Unicode normalization; includes title",
    "length_steps": [
        "Remove every URL token matched by the published URL regex.",
        "Remove exact known source_id tokens and tokens matched by the unknown-S-ID regex.",
        "Remove line-start Markdown bullet/ordered-list markers and horizontal-rule-only lines.",
        "Remove the published Markdown symbol characters globally.",
        "Remove all Unicode whitespace using str.isspace(), then count Unicode code points.",
        "Punctuation, source outlet names, Latin letters, digits and combining marks otherwise each count.",
    ],
    "markdown_symbols_removed": MARKDOWN_SYMBOLS,
    "url_pattern": URL_PATTERN,
    "url_comparison": "case-sensitive exact URL after stripping trailing ASCII . , ; : ! ?; no redirects or normalization",
    "source_id_comparison": "case-sensitive exact source_id with Unicode-word/hyphen token boundaries",
    "unknown_source_id_pattern": S_ID_PATTERN,
    "citation_status_precedence": ["unknown_source_id", "valid", "missing", "unobserved"],
    "citation_rules": [
        "valid means at least one exact known source_id or known full URL, with no unknown S-ID.",
        "unknown_source_id means any S-ID regex token absent from this task's known source_ids.",
        "missing means an observed draft has no exact known ID or URL and no unknown S-ID.",
        "Institution names, article titles and natural-language attributions are not counted as explicit matches.",
        "Unknown URLs are listed independently; a valid label never establishes claim-level citation support.",
    ],
    "residual_reference_pattern": REF_PATTERN,
    "draft_selection": [
        "completed: verified draft.md",
        "failed: verified draft.partial.md if present; never promote to a completed draft",
        "unknown/not_run: no draft analysis; raw incomplete artifacts are not silently accepted",
    ],
    "denominator": "Every planned arm remains in state and signal counts, including failed/unknown/not_run.",
    "prohibited_inferences": [
        "No factual correctness, critical_error, usable rate, human approval or quality/release gate.",
        "A matching citation can still be irrelevant or factually unsupported.",
        "A length, reference or stop signal cannot substitute for semantic human review.",
    ],
}

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def source_id_regex(identifier):
    return re.compile(r"(?<![\w-])" + re.escape(identifier) + r"(?![\w-])")

def url_tokens(text):
    return sorted({match.group(0).rstrip(".,;:!?") for match in URL_RE.finditer(text)})

def count_length(text, known_source_ids):
    value = URL_RE.sub("", text)
    for identifier in sorted(known_source_ids, key=lambda item: (-len(item), item)):
        value = source_id_regex(identifier).sub("", value)
    value = S_ID_RE.sub("", value)
    value = re.sub(r"(?m)^[ \t]*[-*_]{3,}[ \t]*$", "", value)
    value = re.sub(r"(?m)^[ \t]*(?:[-+*]|\d+[.)])[ \t]+", "", value)
    value = value.translate(str.maketrans("", "", MARKDOWN_SYMBOLS))
    return len("".join(character for character in value if not character.isspace()))

def analyze_text(text, documents):
    known_ids = {document["source_id"] for document in documents}
    known_urls = {document["url"] for document in documents}
    matched_ids = sorted(identifier for identifier in known_ids if source_id_regex(identifier).search(text))
    observed_s_ids = set(S_ID_RE.findall(text))
    unknown_ids = sorted(observed_s_ids - known_ids)
    observed_urls = set(url_tokens(text))
    matched_urls = sorted(observed_urls & known_urls)
    unknown_urls = sorted(observed_urls - known_urls)
    citation = "unknown_source_id" if unknown_ids else "valid" if matched_ids or matched_urls else "missing"
    characters = count_length(text, known_ids)
    return {
        "citation_status": citation,
        "explicit_known_source_match": bool(matched_ids or matched_urls),
        "matched_source_ids": matched_ids,
        "matched_source_urls": matched_urls,
        "unknown_source_ids": unknown_ids,
        "unknown_urls": unknown_urls,
        "residual_ref_marker": bool(REF_RE.search(text)),
        "unicode_characters": characters,
        "length_within_250_350": 250 <= characters <= 350,
    }

def unobserved_text_signals():
    return {"citation_status": "unobserved", "explicit_known_source_match": None,
            "matched_source_ids": [], "matched_source_urls": [], "unknown_source_ids": [], "unknown_urls": [],
            "residual_ref_marker": None, "unicode_characters": None, "length_within_250_350": None}

def boolean_counts(records, key):
    values = [record["signals"][key] for record in records]
    return {"true": sum(value is True for value in values),
            "false": sum(value is False for value in values),
            "unobserved": sum(value is None for value in values),
            "planned_denominator": len(records)}

def summarize(records):
    result = {}
    for arm in ARMS:
        selected = [record for record in records if record["arm"] == arm]
        if len(selected) != 100:
            raise ValueError("each arm must retain exactly 100 planned requests")
        result[arm] = {
            "planned_denominator": 100,
            "generation_states": {name: sum(record["generation_status"] == name for record in selected)
                                  for name in ("completed", "failed", "unknown", "not_run")},
            "draft_artifacts_observed": sum(record["draft_sha256"] is not None for record in selected),
            "signals": {key: boolean_counts(selected, key) for key in
                        ("terminal", "finish_stop", "explicit_known_source_match",
                         "residual_ref_marker", "length_within_250_350")},
            "citation_status": {name: sum(record["signals"]["citation_status"] == name for record in selected)
                                for name in ("valid", "missing", "unknown_source_id", "unobserved")},
        }
    return result

def collect(run_dir, dataset=None):
    """Read only verified terminal artifacts; never contact HTTP or alter the run."""
    run_dir = Path(run_dir).resolve()
    if dataset is None:
        plan, rows = verify(run_dir)
        execution = status(run_dir)
        verification_mode = "local_frozen_runner"
    else:
        from evaluation.verify_writing_run_portable import verify_portable, status_portable
        plan, rows = verify_portable(run_dir, Path(dataset).resolve())
        execution = status_portable(run_dir, Path(dataset).resolve())
        verification_mode = "portable_read_only"
    if plan.get("planned_tasks") != 100 or plan.get("planned_requests") != 200:
        raise ValueError("this algorithm requires a frozen 100-task/200-request run")
    requests = plan.get("requests")
    if not isinstance(requests, list) or len(requests) != 200:
        raise ValueError("planned request denominator mismatch")
    states = execution.get("requests", [])
    state_map = {item["request_id"]: item for item in states}
    if len(state_map) != 200 or set(state_map) != {item["request_id"] for item in requests}:
        raise ValueError("status must account for all 200 planned requests")
    row_map = {row["case_id"]: row for row in rows}
    records = []
    for request in requests:
        folder = case_dir(run_dir, request)
        state = state_map[request["request_id"]]["status"]
        if state not in ("completed", "failed", "unknown", "not_run"):
            raise ValueError("unsupported generation state")
        signals = unobserved_text_signals()
        artifact, kind, raw_sha, finish_reason = None, None, None, None
        metadata = read(folder / "metadata.json") if state in ("completed", "failed") else {}
        if state == "completed":
            artifact, kind = folder / "draft.md", "completed_draft"
        elif state == "failed" and metadata.get("partial_draft_sha256"):
            artifact, kind = folder / "draft.partial.md", "failed_partial_draft"
        if artifact is not None:
            expected = metadata.get("draft_sha256" if state == "completed" else "partial_draft_sha256")
            raw_sha = file_sha(artifact)
            if raw_sha != expected:
                raise ValueError("original draft SHA mismatch")
            text = artifact.read_text(encoding="utf-8")
            signals = analyze_text(text, row_map[request["case_id"]]["input"]["requirements"]["source_documents"])
        if state in ("completed", "failed"):
            response_file = folder / "response.json"
            if response_file.is_file():
                if file_sha(response_file) != metadata.get("response_sha256"):
                    raise ValueError("response SHA mismatch")
                response = read(response_file)
                choices = response.get("choices") if isinstance(response, dict) else None
                if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
                    finish_reason = choices[0].get("finish_reason")
            signals["terminal"] = True
        else:
            signals["terminal"] = False
        signals["finish_stop"] = (finish_reason == "stop") if finish_reason is not None else None
        records.append({
            "case_id": request["case_id"], "event_group_id": request["event_group_id"],
            "arm": request["arm"], "request_id": request["request_id"],
            "generation_status": state, "finish_reason": finish_reason,
            "draft_kind": kind, "draft_path": str(artifact.relative_to(run_dir)) if artifact else None,
            "draft_sha256": raw_sha, "signals": signals,
        })
    return {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": ALGORITHM["scope"], "algorithm": ALGORITHM,
        "algorithm_sha256": hashlib.sha256(canonical(ALGORITHM)).hexdigest(),
        "tool_source_sha256": file_sha(Path(__file__)),
        "runner_source_sha256": plan["source_sha256"],
        "run_dir": str(run_dir), "run_id": plan["run_id"], "verification_mode": verification_mode,
        "plan_sha256": file_sha(run_dir / "plan.json"),
        "planned_tasks": 100, "planned_event_groups": plan["planned_event_groups"], "planned_requests": 200,
        "human_quality_scoring": "not_performed", "human_editing_time": None,
        "summary_by_arm": summarize(records), "records": records,
    }

def render_markdown(report):
    lines = [
        "# Writing output format / source traceability signals",
        "",
        "These are deterministic format and explicit-source-traceability signals only.",
        "They are not factual correctness, usable rate, human review or a quality/release gate.",
        "No semantic error labels or editing times are inferred.",
        "",
        "Algorithm: " + ALGORITHM_VERSION,
        "",
        "- Fixed denominator: 100 tasks and 200 planned drafts; 100 requests per arm.",
        "- Include titles. Remove URL tokens, exact known source IDs and S-ID regex tokens,",
        "  line-start Markdown list markers/rules, the published Markdown symbol set, and Unicode whitespace.",
        "- Count remaining Python Unicode code points (not words); 250 to 350 inclusive.",
        "- Exact source IDs/full URLs only; an institution name is not an explicit match.",
        "- Unknown S-IDs take precedence over valid; matching a source does not establish factual support.",
        "- Failed partial drafts are labelled; absent/unknown drafts have unobserved text signals.",
        "- A retained failed or unknown request never disappears from the planned denominator.",
        "",
        "Full regexes and step order are recorded in audit.json. Freeze the algorithm and tool SHA before reading evaluation drafts.",
        "",
        "| Arm | Planned | Completed | Failed | Unknown | Not run |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm in ARMS:
        item = report["summary_by_arm"][arm]
        states = item["generation_states"]
        lines.append(f"| {arm} | 100 | {states['completed']} | {states['failed']} | {states['unknown']} | {states['not_run']} |")
    lines += ["", "| Arm | Signal | True | False | Unobserved | Planned |",
              "| --- | --- | ---: | ---: | ---: | ---: |"]
    for arm in ARMS:
        for key, counts in report["summary_by_arm"][arm]["signals"].items():
            lines.append(f"| {arm} | {key} | {counts['true']} | {counts['false']} | {counts['unobserved']} | 100 |")
    lines += ["", "| Arm | Citation status | Count / planned 100 |", "| --- | --- | ---: |"]
    for arm in ARMS:
        for key, count in report["summary_by_arm"][arm]["citation_status"].items():
            lines.append(f"| {arm} | {key} | {count} |")
    lines += ["", "Per-request original artifact SHA and signals:", "",
              "| Case | Arm | State | Draft kind | Original SHA256 | Finish | Citation | Characters | In range | Residual ref |",
              "| --- | --- | --- | --- | --- | --- | --- | ---: | --- | --- |"]
    def cell(value):
        return "-" if value is None else str(value).replace("|", "\\|").replace("\n", " ")
    for record in report["records"]:
        sig = record["signals"]
        values = [record["case_id"], record["arm"], record["generation_status"], record["draft_kind"],
                  record["draft_sha256"], record["finish_reason"], sig["citation_status"],
                  sig["unicode_characters"], sig["length_within_250_350"], sig["residual_ref_marker"]]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    lines += ["", "Algorithm SHA256: " + report["algorithm_sha256"],
              "", "Tool SHA256: " + report["tool_source_sha256"],
              "", "Frozen plan SHA256: " + report["plan_sha256"], ""]
    return "\n".join(lines)

def audit(run_dir, out, dataset=None):
    out = Path(out)
    if out.exists():
        raise ValueError("output exists; choose a new directory")
    report = collect(run_dir, dataset)
    out.mkdir(parents=True, exist_ok=False)
    write_new(out / "audit.json", report)
    with (out / "audit.md").open("x", encoding="utf-8") as handle:
        handle.write(render_markdown(report))
        handle.flush()
        os.fsync(handle.fileno())
    write_new(out / "audit-seal.json", {
        "schema_version": 1, "algorithm_sha256": report["algorithm_sha256"],
        "tool_source_sha256": report["tool_source_sha256"],
        "audit_json_sha256": file_sha(out / "audit.json"), "audit_md_sha256": file_sha(out / "audit.md"),
    })
    return {"status": "exported", "scope": report["scope"], "planned_tasks": 100,
            "planned_requests": 200, "out": str(out.resolve())}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, help="relocated dataset for read-only portable verification")
    args = parser.parse_args(argv)
    try:
        result = audit(args.run_dir, args.out, args.dataset)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, "Output signal audit blocked: " + str(error) + "\n")
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
