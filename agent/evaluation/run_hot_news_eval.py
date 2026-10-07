"""校验热点评测集，或通过 Python 模型运行时执行离线评测。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.evaluation.hot_news import (
    HotNewsEvaluationService,
    load_evaluation_dataset,
)
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.factory import build_model_runtime_ports
from app.model_runtime.hot_news import NativeHotNewsAnalysisRunner


DEFAULT_DATASET = (
    Path(__file__).parent / "datasets" / "hot_news_eval_seed_v1.json"
)


async def main_async() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--run-model",
        action="store_true",
        help="显式请求 YAML 配置的模型接口；默认只校验数据集。",
    )
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=Path("deploy/model-runtime.local.yml"),
    )
    parser.add_argument("--prompt-version", required=False)
    parser.add_argument("--model-route", required=False)
    args = parser.parse_args()

    dataset = load_evaluation_dataset(args.dataset)
    if not args.run_model:
        print(
            json.dumps(
                {
                    "status": "dataset_valid",
                    "dataset_name": dataset.dataset_name,
                    "dataset_version": dataset.dataset_version,
                    "dataset_sha256": dataset.content_sha256,
                    "case_count": len(dataset.cases),
                    "tags": sorted(
                        {tag for case in dataset.cases for tag in case.tags}
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    config = load_model_runtime_config(args.runtime_config)
    prompt_version = args.prompt_version or next(
        item.version for item in config.prompts if item.scene == "hot_news_analysis"
    )
    model_route = args.model_route or config.inference.model_routes[0]
    ports = build_model_runtime_ports(config, environment="e2e")
    try:
        report = await HotNewsEvaluationService(
            NativeHotNewsAnalysisRunner(
                StructuredInferenceService(
                    inference=ports.inference,
                    prompts=ports.prompts,
                ),
                tenant_id="offline-evaluation",
                prompt_version=prompt_version,
                model_route=model_route,
            )
        ).evaluate(dataset)
    finally:
        await ports.close()
    print(report.model_dump_json(indent=2))


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
