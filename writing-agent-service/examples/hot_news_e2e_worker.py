"""使用本地场景数据启动热点分析 Temporal Worker。"""

import asyncio

from app.hot_news_worker import run_hot_news_worker
from examples.hot_news_e2e_support import (
    build_hot_news_e2e_dependencies,
)


async def run() -> None:
    dependencies = build_hot_news_e2e_dependencies()

    await run_hot_news_worker(
        behavior_data_source=dependencies.behavior_data_source,
        baseline_provider=dependencies.baseline_provider,
        content_repository=dependencies.content_repository,
        policy=dependencies.policy,
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()