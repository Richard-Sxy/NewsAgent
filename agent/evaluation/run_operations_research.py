"""Build, validate and score the independent public-research synthetic package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.operations_evolution_eval import score_predictions
from evaluation.operations_research import (
    DEFAULT_CATALOG, DEFAULT_OUTPUT, OperationsResearchDataset, build_dataset,
    load_catalog, summary, verify_artifact_hashes, write_artifacts,
)
from evaluation.run_operations_evolution import _predictions, _write_result


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    build.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    for name in ("validate", "score"):
        command = commands.add_parser(name)
        command.add_argument("--dataset", type=Path, default=DEFAULT_OUTPUT / "operations_cases.json")
        if name == "validate":
            command.add_argument("--verify-artifacts", action="store_true")
        else:
            command.add_argument("--predictions", type=Path, required=True)
            command.add_argument("--split", choices=("development", "validation", "test"))
            command.add_argument("--board-id")
            command.add_argument("--owner-user-id")
            command.add_argument("--output", type=Path)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "build":
        result = write_artifacts(build_dataset(load_catalog(args.catalog)), args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    dataset = OperationsResearchDataset.model_validate_json(args.dataset.read_bytes())
    if args.command == "validate":
        if args.verify_artifacts:
            verify_artifact_hashes(args.dataset.parent)
        print(json.dumps({"status": "dataset_valid", **summary(dataset)}, ensure_ascii=False, indent=2))
        return 0
    result = score_predictions(dataset, _predictions(args.predictions), board_id=args.board_id,
                              split=args.split, owner_user_id=args.owner_user_id)
    result["design_basis"] = "public_research_with_synthetic_news_facts"
    result["manual_semantic_review_required"] = True
    _write_result(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
