from types import SimpleNamespace

from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.services.hot_news_analysis import HotNewsAnalysisService
from app.services.memory_aware_hot_news_analysis import (
    MemoryAwareHotNewsAnalysisService,
)


def test_runtime_exposes_memory_aware_analysis_service(monkeypatch) -> None:
    import app.hot_news_bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "FastGPTClient", lambda settings: object())
    monkeypatch.setattr(
        bootstrap,
        "FastGPTKnowledgeSearchClient",
        lambda settings: object(),
    )

    runtime = bootstrap.create_hot_news_runtime(
        SimpleNamespace(
            fastgpt_hot_news_app_id="hot-news-app",
            fastgpt_dataset_id="dataset-1",
        ),
        behavior_data_source=object(),
        baseline_provider=object(),
        content_repository=object(),
        policy=SimpleNamespace(),
    )

    assert isinstance(runtime.service.analysis_service, HotNewsAnalysisService)
    assert isinstance(
        runtime.memory_aware_analysis_service,
        MemoryAwareHotNewsAnalysisService,
    )
    assert isinstance(runtime.service.enrichment_service, HotNewsEnrichmentService)
    assert (
        runtime.memory_aware_analysis_service._analysis_service
        is runtime.service.analysis_service
    )
