import httpx
import pytest
import re

from crawler.tencent_news import NewsArticle
from service.fastgpt_client import FastGPTClient, build_collection_name


def test_create_news_collection(monkeypatch):
    article = NewsArticle(
        url="https://news.qq.com/rain/a/test",
        title="测试新闻",
        publish_time="2026-08-17 10:00:00",
        author="腾讯新闻",
        content="测试正文",
    )

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs

        request = httpx.Request("POST", url)

        return httpx.Response(
            status_code=200,
            request=request,
            json={
                "code": 200,
                "data": {
                    "collectionId": "fake_collection_id",
                    "results": {
                        "insertLen": 2,
                    },
                },
            },
        )

    monkeypatch.setattr(
        "service.fastgpt_client.httpx.post",
        fake_post,
    )

    client = FastGPTClient()

    result = client.create_news_collection(
        article=article,
        text="# 测试新闻\n\n测试正文",
        extra_metadata={
            "topic": "科技",
            "source_url": "不允许覆盖",
        },
    )
    assert result["collectionId"] == "fake_collection_id"
    assert captured["url"].endswith(
        "/api/core/dataset/collection/create/text"
    )

    payload = captured["kwargs"]["json"]

    assert payload["name"] == "tencent-news-test"
    assert payload["text"] == "# 测试新闻\n\n测试正文"
    assert payload["trainingType"] == "chunk"
    assert payload["chunkSettingMode"] == "auto"
    assert payload["metadata"]["source_url"] == article.url
    assert payload["metadata"]["news_id"] == "test"
    assert payload["metadata"]["original_title"] == article.title
    assert payload["metadata"]["topic"] == "科技"


def test_build_collection_name_sanitizes_article_id():
    article = NewsArticle(
        url="https://news.qq.com/rain/a/测试%20article",
        title="包含/特殊字符的标题",
        publish_time="",
        author="",
        content="测试正文",
    )

    name = build_collection_name(article)

    assert name == "tencent-news-20article"
    assert re.fullmatch(r"[A-Za-z0-9_-]+", name)


def test_create_news_collection_exposes_http_error(monkeypatch):
    article = NewsArticle(
        url="https://news.qq.com/rain/a/test",
        title="测试新闻",
        publish_time="",
        author="",
        content="测试正文",
    )

    def fake_post(url, **kwargs):
        return httpx.Response(
            status_code=500,
            request=httpx.Request("POST", url),
            json={
                "code": 500,
                "message": "向量模型不可用",
            },
        )

    monkeypatch.setattr(
        "service.fastgpt_client.httpx.post",
        fake_post,
    )

    with pytest.raises(
        RuntimeError,
        match="FastGPT HTTP 500.*向量模型不可用",
    ):
        FastGPTClient().create_news_collection(
            article=article,
            text="测试正文",
        )


def test_create_news_qa_collection(monkeypatch):
    article = NewsArticle(
        url="https://news.qq.com/rain/a/QA001",
        title="QA测试新闻",
        publish_time="2026-08-19 10:00:00",
        author="腾讯新闻",
        content="测试正文" * 20,
    )
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "code": 200,
                "data": {"collectionId": "qa-collection-1", "results": {"insertLen": 0}},
            },
        )

    monkeypatch.setattr("service.fastgpt_client.httpx.post", fake_post)
    monkeypatch.setattr(
        "service.fastgpt_client.settings.fastgpt_qa_dataset_id",
        "qa-dataset-1",
    )

    result = FastGPTClient().create_news_qa_collection(
        article,
        "# QA测试新闻\n\n正文",
        {"topic": "科技"},
    )

    payload = captured["kwargs"]["json"]
    assert result["collectionId"] == "qa-collection-1"
    assert payload["datasetId"] == "qa-dataset-1"
    assert payload["trainingType"] == "qa"
    assert payload["qaPrompt"]
    assert payload["metadata"]["topic"] == "科技"
    assert payload["metadata"]["source_url"] == article.url
    assert payload["metadata"]["news_id"] == "QA001"
