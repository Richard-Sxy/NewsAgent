"""并发执行分析：控制并发数量、保持结果顺序、清理失败任务并记录耗时。"""

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic_ns
from collections.abc import Sequence

from app.analytics.hot_news_enrichment import EnrichedHotNews
from app.services.hot_news_analysis import HotNewsAnalysisExecution, HotNewsAnalysisService


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HotNewsAnalysisBatchStats:
    executor_version: str
    max_concurrency: int
    observed_concurrency: int
    task_count: int
    elapsed_ms: int


class HotNewsAnalysisBatch:
    """Fan out independent snapshots, then join in input order or fail as a unit.

    Share this instance across run-scoped orchestration services. Its semaphore
    covers model analysis, including each item's existing validation retry.
    It does not hold a database session or introduce another retry layer.
    """

    def __init__(self, *, max_concurrency: int = 1) -> None:
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 16:
            raise ValueError("analysis max_concurrency must be an integer between 1 and 16")
        self.max_concurrency = max_concurrency
        self._slots = asyncio.Semaphore(max_concurrency)

    async def analyze(
        self, items: Sequence[EnrichedHotNews], *, service: HotNewsAnalysisService,
    ) -> tuple[list[HotNewsAnalysisExecution], HotNewsAnalysisBatchStats]:
        started = monotonic_ns()
        results: list[HotNewsAnalysisExecution | None] = [None] * len(items)
        pending = iter(enumerate(items))
        active = peak = 0
        stopped = False
        status = "completed"

        async def worker() -> None:
            nonlocal active, peak, stopped
            while not stopped:
                try:
                    index, item = next(pending)
                except StopIteration:
                    return
                async with self._slots:
                    if stopped:
                        return
                    active += 1
                    peak = max(peak, active)
                    try:
                        results[index] = await service.analyze_with_snapshot(item)
                    except BaseException:
                        stopped = True
                        raise
                    finally:
                        active -= 1

        tasks: list[asyncio.Task] = []
        try:
            if self.max_concurrency == 1:
                # Preserve serial failure semantics and avoid task overhead.
                await worker()
            else:
                tasks = [asyncio.create_task(worker(), name=f"hot-news-analysis-worker-{index}")
                         for index in range(min(self.max_concurrency, len(items)))]
                await asyncio.gather(*tasks)
            if any(result is None for result in results):
                raise RuntimeError("hot-news analysis batch is incomplete")
        except BaseException as exc:
            stopped = True
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            # Drain every task before the Activity can retry or save a result.
            # Re-raise the original typed error, not an ExceptionGroup.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            stats = HotNewsAnalysisBatchStats(
                executor_version="bounded-analysis-v1", max_concurrency=self.max_concurrency,
                observed_concurrency=peak, task_count=len(items),
                elapsed_ms=(monotonic_ns() - started) // 1_000_000,
            )
            logger.info(
                "hot news analysis batch %s: tasks=%d limit=%d peak=%d elapsed_ms=%d",
                status, stats.task_count, stats.max_concurrency,
                stats.observed_concurrency, stats.elapsed_ms,
            )
        return [result for result in results if result is not None], stats
