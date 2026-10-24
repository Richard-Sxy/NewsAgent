"""Prepare, transport, execute and score query-understanding evaluations."""
from __future__ import annotations

import argparse
import ast
import asyncio
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys
import zipfile

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from evaluation.query_semantics_common import (canonical_sha, load_package, read_json, read_jsonl,
    sha256_file, validate_cases, validate_gold, write_json, write_jsonl)

DEPENDENCIES = ("pydantic", "pydantic-settings", "httpx", "PyYAML", "opentelemetry-api",
                "opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-http")
SOURCE_ROOTS = (
    "evaluation.run_query_semantics", "evaluation.query_semantics_common",
    "evaluation.query_semantics_runner", "evaluation.query_semantics_scoring",
    "evaluation.query_semantics_local",
)


def source_closure() -> dict[str, str]:
    """Copy only Python source reachable from this harness, never .env/data/weights."""
    pending, found = list(SOURCE_ROOTS), {}
    while pending:
        module = pending.pop()
        if module.split(".")[0] not in {"app", "evaluation"}:
            continue
        path = AGENT_ROOT.joinpath(*module.split(".")).with_suffix(".py")
        if not path.exists():
            path = AGENT_ROOT.joinpath(*module.split("."), "__init__.py")
        if not path.is_file() or path.relative_to(AGENT_ROOT).as_posix() in found:
            continue
        relative = path.relative_to(AGENT_ROOT).as_posix()
        found[relative] = sha256_file(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
                    base = base[:len(base) - node.level + 1]
                    imported = ".".join(base + ([node.module] if node.module else []))
                else:
                    imported = node.module or ""
                pending.append(imported)
                pending.extend(imported + "." + alias.name for alias in node.names)
        for parent in path.parents:
            if parent == AGENT_ROOT:
                break
            if (parent / "__init__.py").is_file():
                pending.append(".".join(parent.relative_to(AGENT_ROOT).parts))
    return dict(sorted(found.items()))


def selected_runtime() -> dict:
    from app.model_runtime.config_file import load_model_runtime_config
    config = load_model_runtime_config(AGENT_ROOT / "deploy/model-runtime.local.yml")
    binding = config.agent_scene("query_understanding")
    prompt = next(p for p in config.prompts if (p.scene, p.version) == (binding.scene, binding.prompt_version))
    # Frozen prompt only. No live endpoint or unrelated scene is copied.
    return {"schema_version": 1, "prompts": [prompt.model_dump(mode="json")],
            "agent_scenes": [binding.model_dump(mode="json")],
            "inference": {"provider": "local", "model_routes": [binding.model_route]},
            "embedding": {"provider": "local", "model_routes": ["unused-query-eval-embedding"]}}


def prepare(cases_path: Path, labels_path: Path, split: str, out: Path) -> dict:
    from app.modules.conversation.query_understanding import QueryUnderstanding
    cases, labels = read_jsonl(cases_path), read_jsonl(labels_path)
    validate_cases(cases)
    validate_gold(cases, labels)
    selected = [row for row in cases if row["split"] == split]
    if not selected or split == "train":
        raise ValueError("select a nonempty development/validation/test split")
    ids = {row["id"] for row in selected}
    gold = [row for row in labels if row["id"] in ids]
    review_status = validate_gold(selected, gold)
    runtime, sources = selected_runtime(), source_closure()
    if any(f"evaluation/{name}.py" not in sources for name in (
        "query_semantics_common", "query_semantics_runner", "query_semantics_scoring", "run_query_semantics")):
        raise ValueError("evaluation source files are incomplete")
    out.mkdir(parents=True, exist_ok=False)
    write_jsonl(out / "inputs.jsonl", selected)
    write_jsonl(out / "private/labels.jsonl", gold)
    write_json(out / "runtime.json", runtime)
    write_json(out / "schema.json", QueryUnderstanding.model_json_schema())
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=AGENT_ROOT,
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        head = None
    manifest = {
        "schema_version": 1, "dataset_id": canonical_sha({"inputs": selected, "gold": gold}),
        "split": split, "review_status": review_status,
        "case_ids": [row["id"] for row in selected], "planned": len(selected),
        "groups": sorted({row["group_id"] for row in selected}),
        "cohorts": {cohort: sum(row["cohort"] == cohort for row in selected)
                    for cohort in ("golden", "fresh", "high_risk")},
        "input_sha256": sha256_file(out / "inputs.jsonl"),
        "gold_sha256": sha256_file(out / "private/labels.jsonl"),
        "runtime_sha256": sha256_file(out / "runtime.json"),
        "schema_sha256": sha256_file(out / "schema.json"),
        "prompt_sha256": sha256(runtime["prompts"][0]["system_prompt"].encode()).hexdigest(),
        "prompt_version": runtime["prompts"][0]["version"],
        "source_files_sha256": sources, "git_head": head,
        "source_identity_note": "Per-file hashes include current uncommitted contents; HEAD alone is insufficient.",
        "scoring": {"main": "all ready-target raw slots exact match, including response_style",
                    "error_policy": "execution errors stay in denominator; missing evidence blocks",
                    "repetitions": 1, "thresholds": "unapproved"},
        "scope": {"entry": "query_understanding", "exit": "QueryUnderstanding + resolve_structured_query",
                  "excluded": ["SQL execution", "warehouse", "vector retrieval", "report generation", "training", "production activation"]},
        "production_acceptance": "not_approved",
    }
    write_json(out / "manifest.json", manifest)
    load_package(out)
    return {"package": str(out), "planned": len(selected), "review_status": review_status,
            "model_quality": "not_run", "resume_A": None, "resume_B": None}


def export_train(cases_path: Path, labels_path: Path, holdout_path: Path, package: Path, out: Path) -> dict:
    """Export reviewed messages; does not load a model or start a training job."""
    from app.model_runtime.config_file import ModelRuntimeConfig
    from app.model_runtime.core import StructuredInferenceService
    from app.modules.conversation.query_understanding import QueryUnderstanding
    from evaluation.query_semantics_runner import _payload

    _, evaluation_cases, _ = load_package(package)
    cases, gold, holdouts = read_jsonl(cases_path), read_jsonl(labels_path), read_jsonl(holdout_path)
    validate_cases(cases + holdouts)
    if not cases or any(row["split"] != "train" for row in cases):
        raise ValueError("training file must contain train only")
    if not holdouts or any(row["split"] not in {"development", "validation", "test"} for row in holdouts):
        raise ValueError("supply all disjoint development/validation/test inputs")
    heldout_by_id = {row["id"]: row for row in holdouts}
    if any(heldout_by_id.get(row["id"]) != row for row in evaluation_cases):
        raise ValueError("holdout ledger must include the frozen evaluation inputs unchanged")
    if validate_gold(cases, gold) != "approved":
        raise ValueError("training targets need independent human review")
    config = ModelRuntimeConfig.model_validate(read_json(package / "runtime.json"))
    binding = config.agent_scene("query_understanding")
    # render_input never touches inference; there is deliberately no model Port here.
    service = StructuredInferenceService(inference=None, prompts=config.prompt_registry())
    labels = {row["id"]: row for row in gold}
    messages = []
    for case in cases:
        payload, _, _ = _payload(case)
        rendered = service.render_input(scene=binding.scene, prompt_version=binding.prompt_version,
                                        payload=payload, output_type=QueryUnderstanding)
        messages.append({"messages": [{"role": m.role, "content": m.content} for m in rendered.messages] + [
            {"role": "assistant", "content": json.dumps(labels[case["id"]]["target"], ensure_ascii=False)}]})
    out.mkdir(parents=True, exist_ok=False)
    write_jsonl(out / "train.messages.jsonl", messages)
    manifest = {"training_started": False, "count": len(cases),
                "group_ids": sorted({c["group_id"] for c in cases}),
                "case_ids": [c["id"] for c in cases],
                "inputs_sha256": sha256_file(cases_path), "labels_sha256": sha256_file(labels_path),
                "holdouts_sha256": sha256_file(holdout_path),
                "package_manifest_sha256": sha256_file(package / "manifest.json"),
                "messages_sha256": sha256_file(out / "train.messages.jsonl"),
                "human_review": "approved", "loss_scope_required": "assistant_only"}
    write_json(out / "training-manifest.json", manifest)
    return manifest


def bundle(package: Path, archive: Path, include_gold: bool = False) -> dict:
    manifest, _, _ = load_package(package)
    if archive.exists():
        raise ValueError("archive already exists")
    requirements = "\n".join(f"{name}=={version(name)}" for name in DEPENDENCIES) + "\n"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as z:
        for relative in manifest["source_files_sha256"]:
            z.write(AGENT_ROOT / relative, relative)
        for path in package.rglob("*"):
            if path.is_file() and (include_gold or "private" not in path.relative_to(package).parts):
                z.write(path, "package/" + path.relative_to(package).as_posix())
        z.writestr("requirements.txt", requirements)
        z.write(AGENT_ROOT / "evaluation/QUERY_SEMANTICS.md", "README.md")
        for path in (AGENT_ROOT / "evaluation/config/query_semantics").glob("*.json"):
            z.write(path, "config/" + path.name)
    return {"archive": str(archive), "sha256": sha256_file(archive),
            "gold_included": include_gold, "source_files": len(manifest["source_files_sha256"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--split", choices=("development", "validation", "test"), required=True)
    p.add_argument("--out", type=Path, required=True)
    p = commands.add_parser("validate")
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--public-only", action="store_true")
    p = commands.add_parser("bundle")
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--include-gold", action="store_true", help="optional offline scoring on the other machine")
    p = commands.add_parser("export-train")
    for name in ("cases", "labels", "holdout-cases", "package", "out"):
        p.add_argument("--" + name, type=Path, required=True)
    for command, connection in (("run", "endpoint"), ("run-local", "config")):
        p = commands.add_parser(command)
        for name in ("package", connection, "out"):
            p.add_argument("--" + name, type=Path, required=True)
        p.add_argument("--max-calls", type=int, required=True)
        p.add_argument("--max-seconds", type=float, required=True)
    p = commands.add_parser("score")
    for name in ("package", "run", "out"):
        p.add_argument("--" + name, type=Path, required=True)
    p = commands.add_parser("compare")
    for name in ("package", "baseline", "candidate", "out"):
        p.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.cases, args.labels, args.split, args.out)
        elif args.command == "validate":
            manifest, cases, _ = load_package(args.package, with_gold=not args.public_only)
            result = {"contract": "passed", "planned": len(cases), "review_status": manifest["review_status"],
                      "model_quality": "not_run"}
        elif args.command == "bundle":
            result = bundle(args.package, args.out, args.include_gold)
        elif args.command == "export-train":
            result = export_train(args.cases, args.labels, args.holdout_cases, args.package, args.out)
        elif args.command == "run":
            from evaluation.query_semantics_runner import run_predictions
            result = asyncio.run(run_predictions(args.package, args.endpoint, args.out, args.max_calls, args.max_seconds))
        elif args.command == "run-local":
            from evaluation.query_semantics_local import run_local_predictions
            result = asyncio.run(run_local_predictions(args.package, args.config, args.out, args.max_calls, args.max_seconds))
        elif args.command == "score":
            from evaluation.query_semantics_scoring import score_run
            result = score_run(args.package, args.run, args.out)
        else:
            from evaluation.query_semantics_scoring import compare_runs
            result = compare_runs(args.package, args.baseline, args.candidate, args.out)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        # Config/model errors must never echo credential-bearing input values.
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__,
                          "message": "Input, identity or file contract failed; check frozen assets and configuration."}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
