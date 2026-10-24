"""Freeze, execute and independently score the native report-stage pilot.

Only ``run`` uses the network. No dotenv, production services or auto approvals.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import getpass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = Path(__file__).parent / "datasets/writing/report_pilot_v1"
DEFAULT_CONFIG = Path(__file__).parent / "config/report-pilot.deepseek.yml"
SCENES = ("research", "outline", "section", "revise", "assemble", "review")
FROZEN_FILES = {"inputs.json", "gold.json"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def seal(path: Path) -> None:
    with path.with_suffix(path.suffix + ".sha256").open("x", encoding="ascii") as stream:
        stream.write(digest(path) + "\n")


def unseal(path: Path):
    if digest(path) != path.with_suffix(path.suffix + ".sha256").read_text().strip():
        raise ValueError(f"artifact changed: {path.name}")
    return read(path)


def dependencies() -> dict:
    return dict(sorted((d.metadata["Name"], d.version)
                       for d in importlib.metadata.distributions() if d.metadata["Name"]))


def source_identity() -> dict:
    paths = {Path(__file__).resolve(), Path(__file__).with_name("report_pilot_core.py"),
             Path(__file__).with_name("report_pilot_scoring.py"),
             ROOT / "agent/app/workflows/contracts.py"}
    for directory in ("model_runtime", "observability", "modules/writing", "agents", "schemas"):
        paths.update((ROOT / "agent/app" / directory).rglob("*.py"))
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(paths) if p.is_file()}


def scoring_identity() -> dict:
    return {str(p.relative_to(ROOT)): digest(p) for p in
            (Path(__file__).resolve(), Path(__file__).with_name("report_pilot_scoring.py"))}


def load_dataset(dataset: Path, split: str) -> tuple[list[dict], list[dict], dict]:
    manifest = read(dataset / "manifest.json")
    for name in ("inputs.jsonl", "private/labels.draft.jsonl"):
        if digest(dataset / name) != manifest["files"][name]["sha256"]:
            raise ValueError(f"dataset differs from manifest: {name}; create a new version")
    all_rows = [json.loads(line) for line in (dataset / "inputs.jsonl").read_text().splitlines() if line.strip()]
    labels = [json.loads(line) for line in (dataset / "private/labels.draft.jsonl").read_text().splitlines() if line.strip()]
    ids = [r["case_id"] for r in all_rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate dataset case IDs")
    event_splits = {}
    for row in all_rows:
        event = row["event_group_id"]
        if event in event_splits and event_splits[event] != row["split"]:
            raise ValueError("event group crosses splits")
        event_splits[event] = row["split"]
    rows = [r for r in all_rows if r["split"] == split]
    if not rows:
        raise ValueError("selected split contains no cases")
    for row in rows:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", row["case_id"]):
            raise ValueError("case ID is not a safe artifact name")
        if set(row) != {"case_id", "event_group_id", "split", "input"}:
            raise ValueError("unexpected case fields; labels must remain private")
        if set(row["input"]) != {"topic", "requirements"}:
            raise ValueError("unexpected model input fields")
        req = row["input"]["requirements"]
        allowed = {"language", "audience", "target_word_count", "max_sections", "as_of",
                   "task_description", "source_documents"}
        if set(req) != allowed:
            raise ValueError("unexpected report requirements; use the material-only contract")
        if not isinstance(req["max_sections"], int) or isinstance(req["max_sections"], bool) or not 1 <= req["max_sections"] <= 2:
            raise ValueError("pilot permits one or two sections")
        for doc in req["source_documents"]:
            if set(doc) != {"source_id", "news_id", "url", "title", "published_at", "content"}:
                raise ValueError("unexpected source document fields")
            if not isinstance(doc["content"], str) or not doc["content"].strip():
                raise ValueError("source document has no content")
        if len(canonical(row["input"])) > 100_000:
            raise ValueError("report input exceeds 100 KB pilot limit")
    selected = {r["case_id"] for r in rows}
    chosen_labels = [r for r in labels if r["case_id"] in selected]
    if {r["case_id"] for r in chosen_labels} != selected or len(chosen_labels) != len(rows):
        raise ValueError("missing or duplicated draft labels")
    return rows, chosen_labels, manifest


def init_labels(args) -> dict:
    from evaluation.report_pilot_scoring import make_gold_template
    rows, labels, _ = load_dataset(args.dataset.resolve(), args.split)
    gold = make_gold_template(rows, labels)
    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    save(args.output_file, gold)
    return {"status": "pending_human_review", "cases": len(rows), "path": str(args.output_file.resolve())}


def safe_config(path: Path):
    from app.model_runtime.config_file import load_model_runtime_config
    # Only the dedicated public evaluation directory is an accepted config source.
    path = path.resolve()
    allowed = Path(__file__).parent.resolve() / "config"
    if not path.is_relative_to(allowed) or path.suffix not in {".yml", ".yaml"}:
        raise ValueError("put a public credential-free YAML under evaluation/config/")
    config = load_model_runtime_config(path)
    for scene in SCENES:
        config.agent_scene(scene)
    endpoint = config.inference
    if (endpoint.provider != "openai_compatible" or
            str(endpoint.url) != "https://api.deepseek.com/v1/chat/completions" or
            endpoint.api_key_env != "DEEPSEEK_API_KEY"):
        raise ValueError("this public-material pilot requires the explicit DeepSeek endpoint/key variable")
    if set(endpoint.extra_body) - {"thinking", "max_tokens"}:
        raise ValueError("unexpected inference body fields")
    cap = endpoint.extra_body.get("max_tokens")
    if type(cap) is not int or not 1 <= cap <= 8192:
        raise ValueError("max_tokens must be an integer in 1..8192")
    return config


def prepare(args) -> dict:
    from evaluation.report_pilot_scoring import validate_gold
    dataset, config_path = args.dataset.resolve(), args.config.resolve()
    rows, _, manifest = load_dataset(dataset, args.split)
    gold = read(args.gold_file)
    validate_gold(gold, rows, require_approved=not args.exploratory)
    for row in rows:
        if len({d["url"] for d in row["input"]["requirements"]["source_documents"]}) < 2:
            raise ValueError("full report cases require two source URLs; complete materials before freezing")
    config = safe_config(config_path)
    max_calls = args.max_calls if args.max_calls is not None else 6 * len(rows)
    if not 1 <= max_calls <= 120 or not 1 <= args.max_wall_seconds <= 7200:
        raise ValueError("budget must be 1..120 calls and 1..7200 wall seconds")
    out = args.output_dir.resolve()
    if out.exists():
        raise ValueError("run directory exists; select a new run ID")
    files = source_identity()
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    diff = subprocess.check_output(["git", "-C", str(ROOT), "diff", "HEAD", "--", *files])
    spec = {
        "schema_version": 1, "run_id": str(uuid.uuid4()), "prepared_at": now(),
        "label": args.label, "mode": "exploratory" if args.exploratory else "reviewed_labels",
        "scope": "provided material -> native Research/Writer/Reviewer first-pass content chain",
        "excludes": ["retrieval", "Temporal", "PG/S3", "human workflow approvals", "CMS", "enterprise inference", "automatic revisions"],
        "dataset": {"path": str(dataset), "manifest_sha256": digest(dataset / "manifest.json"),
                    "version": manifest["dataset_version"], "source_boundary": manifest.get("source_boundary")},
        "case_ids": [r["case_id"] for r in rows], "planned": len(rows), "split": args.split,
        "as_of": {r["case_id"]: r["input"]["requirements"]["as_of"] for r in rows},
        "config": {"path": str(config_path), "sha256": digest(config_path)},
        "inference": config.inference.model_dump(mode="json"),
        "scenes": [s.model_dump(mode="json") for s in config.agent_scenes],
        "prompts": [{"scene": p.scene, "version": p.version,
                     "sha256": hashlib.sha256(p.system_prompt.encode()).hexdigest()} for p in config.prompts],
        "budget": {"max_calls": max_calls, "max_wall_seconds": args.max_wall_seconds,
                   "max_sections": 2, "concurrency": 1, "retries": 0, "revision_rounds": 0,
                   "max_output_tokens": max_calls * config.inference.extra_body["max_tokens"],
                   "currency_limit": None, "currency_note": "No currency estimate; calls/output tokens are bounded, actual usage recorded."},
        "oracle": "reviewed independent gold + human semantic annotations + Decimal arithmetic; fixed denominators",
        "threshold_policy": "descriptive metrics only; no production acceptance threshold approved",
        "git_head": head, "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "source_files": files, "scoring_source_files": scoring_identity(),
        "python": sys.version, "dependencies": dependencies(),
        "production_acceptance": "not_approved", "command": sys.argv,
    }
    out.mkdir(parents=True, exist_ok=False)
    save(out / "inputs.json", rows)
    save(out / "gold.json", gold)
    spec["frozen_files"] = {n: digest(out / n) for n in sorted(FROZEN_FILES)}
    save(out / "run-spec.json", spec)
    seal(out / "run-spec.json")
    return {"status": "prepared", "mode": spec["mode"], "planned": len(rows), "run_dir": str(out)}


def verify(run_dir: Path, *, for_execution: bool = False) -> dict:
    spec = unseal(run_dir / "run-spec.json")
    if spec.get("schema_version") != 1 or set(spec["frozen_files"]) != FROZEN_FILES:
        raise ValueError("unsupported or incomplete run specification")
    for name, expected in spec["frozen_files"].items():
        if digest(run_dir / name) != expected:
            raise ValueError(f"frozen {name} changed; prepare a new experiment")
    rows = read(run_dir / "inputs.json")
    if [r["case_id"] for r in rows] != spec["case_ids"] or len(rows) != spec["planned"]:
        raise ValueError("frozen input selection changed")
    if len(set(spec["case_ids"])) != spec["planned"]:
        raise ValueError("duplicate frozen cases")
    if for_execution:
        config_path = Path(spec["config"]["path"])
        if digest(config_path) != spec["config"]["sha256"]:
            raise ValueError("configuration changed; prepare a new run")
        safe_config(config_path)
        if source_identity() != spec["source_files"]:
            raise ValueError("relevant code changed; prepare a new run")
        if sys.version != spec["python"] or dependencies() != spec["dependencies"]:
            raise ValueError("Python or dependencies changed; prepare again")
    return spec


def read_execution(run_dir: Path, spec: dict) -> tuple[dict, list[dict]]:
    summary = unseal(run_dir / "execution-summary.json")
    if summary["run_id"] != spec["run_id"] or len(summary["cases"]) != spec["planned"]:
        raise ValueError("execution does not match run specification")
    traces = []
    for index, case_id in enumerate(spec["case_ids"]):
        item = summary["cases"][index]
        if item["case_id"] != case_id:
            raise ValueError("execution cases changed")
        path = run_dir / "cases" / case_id / "execution.json"
        if digest(path) != item["execution_sha256"]:
            raise ValueError("execution trace changed")
        trace = read(path)
        if trace["case_id"] != case_id:
            raise ValueError("trace case mismatch")
        traces.append(trace)
    return summary, traces


@asynccontextmanager
async def injected_client(client):
    yield client


async def execute(run_dir: Path, api_key: str, *, client_override=None) -> dict:
    from evaluation.report_pilot_core import RunBudget, open_report_client, run_report_case
    from evaluation.report_pilot_scoring import make_human_review
    spec = verify(run_dir, for_execution=True)
    if not api_key.strip() and client_override is None:
        raise ValueError("API key is empty")
    if (run_dir / "run-start.json").exists():
        raise ValueError("this run already started; no silent retry/resume, prepare a new run")
    rows = read(run_dir / "inputs.json")
    save(run_dir / "run-start.json", {"run_id": spec["run_id"], "started_at": now(),
         "backend": "http" if client_override is None else "injected_test_client"})
    budget = RunBudget(max_calls=spec["budget"]["max_calls"],
                       max_wall_seconds=spec["budget"]["max_wall_seconds"],
                       max_sections=spec["budget"]["max_sections"])
    context = (open_report_client(config_path=Path(spec["config"]["path"]), api_key=api_key)
               if client_override is None else injected_client(client_override))
    traces, aborted = [], None
    try:
        async with context as client:
            for row in rows:
                trace = await run_report_case(row, client, run_dir / "cases" / row["case_id"], budget=budget)
                traces.append(trace)
    except (Exception, asyncio.CancelledError) as exc:
        # Exception text may contain credentials or source content; classify only.
        aborted = type(exc).__name__
    for row in rows[len(traces):]:
        case_dir = run_dir / "cases" / row["case_id"]
        execution_file = case_dir / "execution.json"
        if execution_file.exists():
            trace = read(execution_file)
        else:
            case_dir.mkdir(parents=True, exist_ok=True)
            attempts = []
            attempt_file = case_dir / "attempts.jsonl"
            if attempt_file.exists():
                for line in attempt_file.read_text().splitlines():
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue  # Preserve the original incomplete log for diagnosis.
                    if record.get("status") == "attempt_started":
                        attempts.append(record)
            trace = {"case_id": row["case_id"], "event_group_id": row["event_group_id"],
                     "split": row["split"], "job_id": None,
                     "status": "blocked" if attempts else "not_run", "model_calls": len(attempts),
                     "review_decision": None, "stages": [], "error_type": aborted,
                     "partial_artifacts_require_audit": bool(attempts),
                     "outputs": {"research": None, "outline": None, "sections": [], "draft": None, "review": None}}
            # A partial case directory is retained for diagnosis, not retried.
            save(execution_file, trace)
        traces.append(trace)
    counts = Counter(t["status"] for t in traces)
    summary = {"run_id": spec["run_id"], "finished_at": now(), "planned": len(rows),
               "completed": counts["completed"], "failed": counts["failed"],
               "blocked": counts["blocked"], "not_run": counts["not_run"],
               "status": "completed" if counts["completed"] == len(rows) else "incomplete",
               "backend": "http" if client_override is None else "injected_test_client",
               "model_attempts": budget.attempts, "abort_type": aborted,
               "unattributed_attempts": max(0, budget.attempts - sum(t["model_calls"] for t in traces)),
               "model_quality": "not_scored", "production_acceptance": "not_approved",
               "cases": [{"case_id": t["case_id"], "status": t["status"],
                          "execution_sha256": digest(run_dir / "cases" / t["case_id"] / "execution.json")}
                         for t in traces]}
    save(run_dir / "execution-summary.json", summary)
    seal(run_dir / "execution-summary.json")
    save(run_dir / "human-review.template.json", make_human_review(traces, read(run_dir / "gold.json"), spec["run_id"]))
    return summary


def review_template(args) -> dict:
    from evaluation.report_pilot_scoring import make_human_review
    run_dir = args.run_dir.resolve()
    spec = verify(run_dir)
    _, traces = read_execution(run_dir, spec)
    result = make_human_review(traces, read(run_dir / "gold.json"), spec["run_id"])
    save(args.output_file, result)
    return {"status": "pending_human_review", "path": str(args.output_file.resolve())}


def score(args) -> dict:
    from evaluation.report_pilot_scoring import score_reports, validate_gold
    run_dir = args.run_dir.resolve()
    spec = verify(run_dir)
    if spec["mode"] != "reviewed_labels":
        raise ValueError("exploratory run has no frozen approved gold; approve labels and prepare a new run")
    if scoring_identity() != spec["scoring_source_files"]:
        raise ValueError("scoring code changed; cannot silently rescore under another implementation")
    gold = read(run_dir / "gold.json")
    validate_gold(gold, read(run_dir / "inputs.json"), require_approved=True)
    summary, traces = read_execution(run_dir, spec)
    if summary["backend"] != "http":
        raise ValueError("injected test clients cannot produce real-model quality results")
    if summary["blocked"] or summary["not_run"] or summary.get("unattributed_attempts"):
        raise ValueError("incomplete execution audit or unrun cases block final quality scoring; inspect execution-summary")
    human = read(args.human_review)
    result = score_reports(traces, gold, human, spec["run_id"])
    out = args.output_dir.resolve() if args.output_dir else run_dir / "score-01"
    out.mkdir(parents=True, exist_ok=False)
    save(out / "human-review.json", human)
    scoring_spec = {"run_id": spec["run_id"], "run_dir": str(run_dir), "scored_at": now(),
                    "run_spec_sha256": digest(run_dir / "run-spec.json"),
                    "execution_sha256": digest(run_dir / "execution-summary.json"),
                    "human_review_path": str(out / "human-review.json"),
                    "human_review_sha256": digest(out / "human-review.json"),
                    "source_files": scoring_identity()}
    save(out / "score-spec.json", scoring_spec)
    seal(out / "score-spec.json")
    save(out / "results.json", result)
    seal(out / "results.json")
    lines = ["# 报告生成开发评测", "", f"Run: {spec['run_id']}",
             f"范围：{spec['scope']}", f"分组：{spec['split']}；计划事件数：{spec['planned']}", "",
             "| 指标 | 分子 / 分母 | 比例 |", "| --- | --- | --- |"]
    for name, item in result.get("metrics", {}).items():
        rate = item.get("rate")
        lines.append(f"| {name} | {item['count']} / {item['denominator']} | {'N/A' if rate is None else format(rate, '.2%')} |")
    lines.extend(["", "独立人工判定 + 确定性汇总；不是 Reviewer 自评分。",
                  "首轮生成，无自动修订；未覆盖联网检索、Temporal 审批恢复、企业模型或生产收益。",
                  "样本数量很小，开发集结果不代表独立测试集泛化；生产验收未批准。", ""])
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return {"status": result.get("status"), "output_dir": str(out), "metrics": result.get("metrics")}


def load_scored(path: Path) -> tuple[dict, dict, dict]:
    audit = unseal(path / "score-spec.json")
    result = unseal(path / "results.json")
    run_dir = Path(audit["run_dir"])
    spec = verify(run_dir)
    if (digest(run_dir / "run-spec.json") != audit["run_spec_sha256"] or
            digest(run_dir / "execution-summary.json") != audit["execution_sha256"] or
            digest(Path(audit["human_review_path"])) != audit["human_review_sha256"]):
        raise ValueError("scored evidence changed")
    read_execution(run_dir, spec)
    return spec, result, audit


def compare(args) -> dict:
    baseline, before, audit_b = load_scored(args.baseline.resolve())
    candidate, after, audit_c = load_scored(args.candidate.resolve())
    for key in ("case_ids", "split", "as_of", "budget", "inference", "frozen_files"):
        if baseline[key] != candidate[key]:
            raise ValueError(f"unpaired comparison: {key} differs")
    if {s["scene"]: s["model_route"] for s in baseline["scenes"]} != {s["scene"]: s["model_route"] for s in candidate["scenes"]}:
        raise ValueError("unpaired comparison: per-scene model route differs")
    if audit_b["source_files"] != audit_c["source_files"]:
        raise ValueError("scoring implementation differs")
    if before.get("status") != "scored" or after.get("status") != "scored":
        raise ValueError("comparison requires completed independent scoring")
    deltas = {}
    for name, current in before["metrics"].items():
        new = after["metrics"][name]
        eligible_before = {r["case_id"]: r["metrics"][name]["denominator"] for r in before["cases"] if r["metrics"][name]["denominator"]}
        eligible_after = {r["case_id"]: r["metrics"][name]["denominator"] for r in after["cases"] if r["metrics"][name]["denominator"]}
        same_denominators = eligible_before == eligible_after
        difference = (None if not same_denominators or current["rate"] is None or new["rate"] is None
                      else new["rate"] - current["rate"])
        deltas[name] = {"baseline": current, "candidate": new, "delta_percentage_points":
                        None if difference is None else round(difference * 100, 6),
                        "same_eligible_cases_and_denominators": same_denominators}
    result = {"status": "compared", "baseline_run": baseline["run_id"], "candidate_run": candidate["run_id"],
              "planned": baseline["planned"], "split": baseline["split"], "metrics": deltas,
              "changed_prompt_scenes": [s for s in SCENES if
                  [p for p in baseline["prompts"] if p["scene"] == s] != [p for p in candidate["prompts"] if p["scene"] == s]],
              "source_changes": {k: v for k, v in candidate["source_files"].items() if baseline["source_files"].get(k) != v},
              "production_acceptance": "not_approved", "limitations": [
                  "small development pilot; no confidence intervals", "first pass only; no causal attribution to individual changes",
                  "Reviewer rates use human-labelled naturally generated drafts, not fixed injected-error probes"]}
    args.output_dir.mkdir(parents=True, exist_ok=False)
    save(args.output_dir / "comparison.json", result)
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-labels", help="create a human-editable draft gold file; offline")
    init.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    init.add_argument("--split", choices=("development", "validation", "test"), default="development")
    init.add_argument("--output-file", type=Path, required=True)
    prep = sub.add_parser("prepare", help="freeze inputs/gold/config/code identity; offline")
    prep.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    prep.add_argument("--split", choices=("development", "validation", "test"), default="development")
    prep.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    prep.add_argument("--gold-file", type=Path, required=True)
    prep.add_argument("--output-dir", type=Path, required=True)
    prep.add_argument("--label", choices=("baseline", "candidate"), default="baseline")
    prep.add_argument("--exploratory", action="store_true")
    prep.add_argument("--max-calls", type=int)
    prep.add_argument("--max-wall-seconds", type=float, default=1800)
    run = sub.add_parser("run", help="call real DeepSeek, save stages; no automatic retry")
    run.add_argument("--run-dir", type=Path, required=True)
    review = sub.add_parser("review-template", help="create another independent human review form; offline")
    review.add_argument("--run-dir", type=Path, required=True)
    review.add_argument("--output-file", type=Path, required=True)
    scoring = sub.add_parser("score", help="validate human labels and aggregate quality metrics; offline")
    scoring.add_argument("--run-dir", type=Path, required=True)
    scoring.add_argument("--human-review", type=Path, required=True)
    scoring.add_argument("--output-dir", type=Path)
    comparison = sub.add_parser("compare", help="compare two scored experiments under matched conditions")
    comparison.add_argument("--baseline", type=Path, required=True, help="baseline score directory")
    comparison.add_argument("--candidate", type=Path, required=True, help="candidate score directory")
    comparison.add_argument("--output-dir", type=Path, required=True)
    return p


def main() -> None:
    p = parser()
    args = p.parse_args()
    try:
        if args.command == "run":
            run_dir = args.run_dir.resolve()
            verify(run_dir, for_execution=True)
            key = os.environ.get("DEEPSEEK_API_KEY", "")
            if not key:
                if not sys.stdin.isatty():
                    raise ValueError("set DEEPSEEK_API_KEY in your controlled environment or run interactively")
                key = getpass.getpass("DeepSeek API key (hidden, not saved): ")
            result = asyncio.run(execute(run_dir, key))
        else:
            result = {"init-labels": init_labels, "prepare": prepare, "review-template": review_template,
                      "score": score, "compare": compare}[args.command](args)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        # Inputs/configuration are public material; do not print exception bodies.
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, ValueError) else "Check required files and JSON fields; no existing artifacts overwritten."}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
