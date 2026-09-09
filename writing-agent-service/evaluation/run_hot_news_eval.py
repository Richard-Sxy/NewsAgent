"""校验热点评测集，或显式调用 FastGPT 执行离线评测。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from app.clients.fastgpt import FastGPTClient
from app.evaluation.hot_news import (
    HotNewsEvaluationService,
    load_evaluation_dataset,
)
from app.services.agents.hot_news import HotNewsAnalysisAgentRunner


DEFAULT_DATASET = (
    Path(__file__).parent / "datasets" / "hot_news_eval_seed_v1.json"
)


async def main_async() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--run-model",
        action="store_true",
        help="显式请求 FastGPT；默认只校验数据集，不访问网络。",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("FASTGPT_BASE_URL", "http://127.0.0.1:3000"),
    )
    parser.add_argument("--app-id", default=os.getenv("FASTGPT_HOT_NEWS_APP_ID"))
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

    api_key = os.getenv("FASTGPT_API_KEY")
    if not api_key:
        raise SystemExit("FASTGPT_API_KEY environment variable is required")
    if not args.app_id:
        raise SystemExit("--app-id or FASTGPT_HOT_NEWS_APP_ID is required")

    settings = SimpleNamespace(
        fastgpt_base_url=args.base_url,
        fastgpt_api_key=api_key,
    )
    async with FastGPTClient(settings) as client:
        report = await HotNewsEvaluationService(
            HotNewsAnalysisAgentRunner(client, args.app_id)
        ).evaluate(dataset)
    print(report.model_dump_json(indent=2))


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
