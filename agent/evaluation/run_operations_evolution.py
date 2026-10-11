"""Build, validate, score and compare synthetic news evolution evaluation assets."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile

from app.schemas.evaluation_dataset import canonical_json_sha256
from evaluation.operations_evolution import (
    DEFAULT_CATALOG, DEFAULT_OUTPUT, OperationsEvolutionDataset,
    build_dataset, load_catalog, summary, verify_artifact_hashes, write_artifacts,
)
from evaluation.operations_evolution_eval import (
    EvolutionPrediction, GatePolicy, NativeEvolutionReviewRunner,
    PROMPT_VERSION, SYSTEM_PROMPT, compare_reports, run_reviews, score_predictions, select_cases,
)


def _predictions(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("predictions")
    if not isinstance(raw, list):
        raise ValueError("prediction file must be an array or a run result with predictions")
    return [EvolutionPrediction.model_validate(item) for item in raw]


def _write_result(path, value):
    if path is None:
        print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") == payload:
            return
        raise ValueError("result path already has different content; select a new output path")
    # Exclusive creation avoids silently replacing an existing experiment.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    print(json.dumps({"output": str(path.resolve()), "sha256": canonical_json_sha256(value)}, ensure_ascii=False))


def _add_selection(parser):
    parser.add_argument("--board-id")
    parser.add_argument("--owner-user-id")
    parser.add_argument("--split", choices=("development", "validation", "test"))


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    build.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    for name in ("validate", "score", "compare", "run-model"):
        command = commands.add_parser(name)
        command.add_argument("--dataset", type=Path, default=DEFAULT_OUTPUT / "operations_cases.json")
        if name == "validate":
            command.add_argument("--verify-artifacts", action="store_true")
            continue
        _add_selection(command)
        command.add_argument("--output", type=Path)
        if name == "score":
            command.add_argument("--predictions", required=True, type=Path)
        elif name == "compare":
            command.add_argument("--candidate", required=True, type=Path)
            command.add_argument("--current", required=True, type=Path)
            command.add_argument("--previous", type=Path)
            command.add_argument("--policy", type=Path)
        else:
            command.add_argument("--runtime-config", required=True, type=Path)
            command.add_argument("--model-route")
            command.add_argument("--max-cases", type=int, default=200)
            command.add_argument("--resume", action="store_true", help="Reuse saved attempts from the identical experiment checkpoint.")
            command.set_defaults(split="test")
    return root


async def _run_model(dataset, args, selection):
    # Imports are deferred; offline build/validate/score do not create clients or read dotenv.
    from app.model_runtime.config_file import load_model_runtime_config
    from app.model_runtime.http import OpenAICompatibleInferenceClient

    if args.output is None:
        raise ValueError("run-model requires --output for a durable experiment result")
    if args.output.exists():
        raise ValueError("run-model output already exists; use a new experiment path")
    cases = select_cases(dataset, **selection)
    if not 1 <= args.max_cases <= 2000 or len(cases) > args.max_cases:
        raise ValueError("selected cases exceed the explicit model-call budget")
    config = load_model_runtime_config(args.runtime_config)
    endpoint = config.inference
    route = args.model_route or endpoint.model_routes[0]
    if route not in endpoint.model_routes:
        raise ValueError("model route is not registered in the runtime YAML")
    if endpoint.provider != "openai_compatible":
        raise ValueError("operations review requires a configured inference interface; local analysis fixture has no review scene")
    credential = os.environ.get(endpoint.api_key_env or "", "")
    if not credential.strip():
        raise ValueError("model credential environment variable is missing")
    identity = {"dataset_sha256": canonical_json_sha256(dataset), "selection": selection,
                "runtime_config_sha256": canonical_json_sha256(config), "model_route": route,
                "experiment_id": canonical_json_sha256({"output": str(args.output.resolve())}),
                "prompt_version": PROMPT_VERSION, "prompt_sha256": canonical_json_sha256({"prompt": SYSTEM_PROMPT})}
    checkpoint = args.output.with_name(args.output.name + ".progress.json")
    initial_predictions, initial_errors = [], []
    if args.resume:
        if checkpoint.is_symlink():
            raise ValueError("experiment checkpoint cannot be a symlink")
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if saved.get("identity") != identity:
            raise ValueError("checkpoint input, selection, model or prompt differs")
        initial_predictions = [EvolutionPrediction.model_validate(item) for item in saved["predictions"]]
        initial_errors = saved["errors"]
    elif checkpoint.exists():
        raise ValueError("experiment checkpoint exists; explicitly resume or choose a new path")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save_progress(predictions, errors):
        payload = {"identity": identity, "predictions": [item.model_dump(mode="json") for item in predictions],
                   "errors": errors, "status": "in_progress"}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=checkpoint.parent,
                                         prefix=".operations-progress-", delete=False) as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            temporary = Path(stream.name)
        temporary.replace(checkpoint)
    save_progress(initial_predictions, initial_errors)
    client = OpenAICompatibleInferenceClient(endpoint, api_key=credential)
    try:
        result = await run_reviews(dataset, NativeEvolutionReviewRunner(
            client, model_route=route, timeout_seconds=endpoint.timeout_seconds,
            experiment_id=canonical_json_sha256(identity)), **selection,
            on_case=save_progress, initial_predictions=initial_predictions, initial_errors=initial_errors)
    finally:
        await client.close()
    result["runtime"] = {"provider": endpoint.provider, "model_route": route,
                         "runtime_config_sha256": canonical_json_sha256(config),
                         "evaluation_mode": "configured_model_on_synthetic_data"}
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "build":
        result = write_artifacts(build_dataset(load_catalog(args.catalog)), args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    dataset = OperationsEvolutionDataset.model_validate_json(args.dataset.read_text(encoding="utf-8"))
    if args.command == "validate":
        if args.verify_artifacts:
            verify_artifact_hashes(args.dataset.parent)
        print(json.dumps({"status": "dataset_valid", **summary(dataset)}, ensure_ascii=False, indent=2))
        return 0
    selection = {"board_id": args.board_id, "split": args.split, "owner_user_id": args.owner_user_id}
    if args.command == "score":
        result = score_predictions(dataset, _predictions(args.predictions), **selection)
    elif args.command == "compare":
        reports = [score_predictions(dataset, _predictions(path), **selection)
                   for path in (args.candidate, args.current, args.previous) if path is not None]
        policy = GatePolicy.model_validate_json(args.policy.read_text(encoding="utf-8")) if args.policy else GatePolicy()
        result = {"gate": compare_reports(reports[0], reports[1], reports[2] if len(reports) > 2 else None,
                                          policy=policy),
                  "candidate": reports[0], "current": reports[1],
                  "previous": reports[2] if len(reports) > 2 else None}
    else:
        result = asyncio.run(_run_model(dataset, args, selection))
    _write_result(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
