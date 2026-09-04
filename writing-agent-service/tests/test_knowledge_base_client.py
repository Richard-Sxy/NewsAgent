import json
from types import SimpleNamespace

import httpx
import pytest

from app.clients.knowledge_base import FastGPTKnowledgeSearchClient


@pytest.mark.asyncio
async def test_fastgpt_search_deduplicates_collections_and_excludes_current_news() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/core/dataset/searchTest"
        assert request.headers["Authorization"] == "Bearer secret"
        payload = json.loads(request.content)
        assert payload["datasetId"] == "dataset-1"
        assert payload["text"] == "人工智能 新模型"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "list": [
                        {
                            "id": "chunk-current",
                            "collectionId": "collection-current",
                            "q": "当前新闻",
                            "score": [0.99],
                            "metadata": {"news_id": "news-1001"},
                        },
                        {
                            "id": "chunk-related-1",
                            "collectionId": "collection-related",
                            "q": "历史报道片段一",
                            "score": [0.8, 0.92],
                            "metadata": {
                                "news_id": "news-0900",
                                "original_title": "相关历史报道",
                                "source_url": "https://news.example.com/news-0900",
                                "publish_time": "2026-09-01 09:00:00",
                            },
                        },
                        {
                            "id": "chunk-related-2",
                            "collectionId": "collection-related",
                            "q": "同一报道的第二个切片",
                            "score": [0.7],
                            "metadata": {"news_id": "news-0900"},
                        },
                    ]
                },
            },
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fastgpt.test",
    )
    settings = SimpleNamespace(
        fastgpt_base_url="http://fastgpt.test",
        fastgpt_api_key="secret",
        fastgpt_dataset_id="dataset-1",
    )
    client = FastGPTKnowledgeSearchClient(settings, http_client=http_client)

    results = await client.search_related_news(
        "人工智能 新模型",
        exclude_news_id="news-1001",
    )
    await http_client.aclose()

    assert len(results) == 1
    assert results[0].news_id == "news-0900"
    assert results[0].title == "相关历史报道"
    assert results[0].score == 0.92


def test_fastgpt_search_requires_dataset_id() -> None:
    settings = SimpleNamespace(
        fastgpt_base_url="http://fastgpt.test",
        fastgpt_api_key="secret",
        fastgpt_dataset_id=None,
    )
    with pytest.raises(ValueError, match="FASTGPT_DATASET_ID"):
        FastGPTKnowledgeSearchClient(settings)


@pytest.mark.asyncio
async def test_fastgpt_search_supports_legacy_source_name_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "list": [
                        {
                            "collectionId": "current",
                            "sourceName": "tencent-news-20260827A0C5VA00.txt",
                            "q": "当前报道",
                            "metadata": {},
                        },
                        {
                            "collectionId": "related",
                            "sourceName": "tencent-news-20260830A08CNG00.txt",
                            "q": "关联报道",
                            "score": [{"value": 0.88}],
                        },
                    ]
                },
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = SimpleNamespace(
        fastgpt_base_url="http://fastgpt.test",
        fastgpt_api_key="secret",
        fastgpt_dataset_id="dataset-1",
    )
    client = FastGPTKnowledgeSearchClient(settings, http_client=http_client)

    results = await client.search_related_news(
        "AI 数据中心",
        exclude_news_id="20260827A0C5VA00",
    )
    await http_client.aclose()

    assert len(results) == 1
    assert results[0].news_id == "20260830A08CNG00"
    assert results[0].score == 0.88
