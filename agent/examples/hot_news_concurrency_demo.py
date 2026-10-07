"""Compare serial/concurrent full-domain runs using local model Ports and fake latency.

No database, network, credentials or real model calls are required. The numbers
demonstrate scheduling, not enterprise-model quality or real endpoint throughput.
"""

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.factory import build_model_runtime_ports
from app.model_runtime.hot_news import NativeHotNewsAnalysisRunner
from app.services.hot_news_analysis import HotNewsAnalysisInputBuilder, HotNewsAnalysisService, HotNewsAnalysisValidator
from app.services.hot_news_analysis_batch import HotNewsAnalysisBatch
from app.services.hot_news_orchestration import HotNewsOrchestrationService, HotNewsRunRequest
from examples.hot_news_e2e_support import build_hot_news_e2e_dependencies
from examples.native_hot_news_e2e_knowledge import build_native_e2e_knowledge_search


async def compare(*, concurrency: int = 3, delay_ms: int = 100) -> dict:
    config = load_model_runtime_config(Path(__file__).resolve().parents[1] / "deploy/model-runtime.local.yml")
    ports = build_model_runtime_ports(config, environment="e2e")
    dependencies = build_hot_news_e2e_dependencies()
    prompt_version = next(prompt.version for prompt in config.prompts
                          if prompt.scene == "hot_news_analysis" and prompt.version == "native-e2e-prompt-v1")

    class DelayedLocalInference:
        async def complete(self, request):
            await asyncio.sleep(delay_ms / 1000)
            return await ports.inference.complete(request)

    try:
        knowledge = await build_native_e2e_knowledge_search(
            dependencies, embedding=ports.embedding,
            embedding_version=config.embedding.model_routes[0],
            embedding_batch_size=config.embedding.max_batch_size,
        )
        request = HotNewsRunRequest(
            tenant_id=dependencies.tenant_id, window_start=dependencies.window_start,
            window_end=dependencies.window_end,
            production_bundle_version=dependencies.production_bundle_version,
        )

        async def run(limit):
            service = HotNewsOrchestrationService(
                behavior_data_source=dependencies.behavior_data_source,
                baseline_provider=dependencies.baseline_provider,
                enrichment_service=HotNewsEnrichmentService(
                    content_repository=dependencies.content_repository, knowledge_search=knowledge,
                ),
                analysis_service=HotNewsAnalysisService(
                    input_builder=HotNewsAnalysisInputBuilder(policy_version=prompt_version),
                    runner=NativeHotNewsAnalysisRunner(
                        StructuredInferenceService(inference=DelayedLocalInference(), prompts=ports.prompts),
                        tenant_id=dependencies.tenant_id, prompt_version=prompt_version,
                        model_route=config.inference.model_routes[0],
                    ), validator=HotNewsAnalysisValidator(),
                ), policy=dependencies.policy, analysis_batch=HotNewsAnalysisBatch(max_concurrency=limit),
            )
            return await service.run(request)

        serial = await run(1)
        concurrent = await run(concurrency)
        same_metrics = serial.ranked_news == concurrent.ranked_news
        same_reports = [item.analysis.value for item in serial.analyzed_news] == [
            item.analysis.value for item in concurrent.analyzed_news]
        if not same_metrics or not same_reports:
            raise RuntimeError("concurrency changed business output")
        return {
            "local_simulation": True, "simulated_model_latency_ms": delay_ms,
            "serial": asdict(serial.analysis_execution),
            "concurrent": asdict(concurrent.analysis_execution),
            "analysis_phase_speedup": round(serial.analysis_execution.elapsed_ms /
                                            max(1, concurrent.analysis_execution.elapsed_ms), 2),
            "same_rank_and_metrics": same_metrics, "same_analysis_reports": same_reports,
        }
    finally:
        await ports.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, choices=range(1, 17), default=3)
    parser.add_argument("--delay-ms", type=int, choices=range(1, 10001), default=100)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(compare(concurrency=args.concurrency, delay_ms=args.delay_ms)),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
