"""Build/validate synthetic operator cases, or score an offline prediction file."""

import argparse
import json
from pathlib import Path

from evaluation.operations_synthetic import (
    DEFAULT_CATALOG, DEFAULT_OUTPUT, OperatorPrediction, ScenarioCatalog,
    SyntheticOperationsDataset, build_dataset, score_predictions, summary, verify_artifact_hashes, write_artifacts,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    build.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    validate = commands.add_parser("validate")
    validate.add_argument("--dataset", type=Path, default=DEFAULT_OUTPUT / "operations_cases.json")
    validate.add_argument("--verify-artifacts", action="store_true", help="Verify every file against the sibling manifest.")
    score = commands.add_parser("score")
    score.add_argument("--dataset", type=Path, default=DEFAULT_OUTPUT / "operations_cases.json")
    score.add_argument("--predictions", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        catalog = ScenarioCatalog.model_validate_json(args.catalog.read_text(encoding="utf-8"))
        result = write_artifacts(build_dataset(catalog), args.output)
    else:
        dataset = SyntheticOperationsDataset.model_validate_json(args.dataset.read_text(encoding="utf-8"))
        if args.command == "validate":
            if args.verify_artifacts:
                verify_artifact_hashes(args.dataset.parent)
            result = {"status": "dataset_valid", **summary(dataset)}
        else:
            raw = json.loads(args.predictions.read_text(encoding="utf-8"))
            result = score_predictions(dataset, [OperatorPrediction.model_validate(p) for p in raw])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
