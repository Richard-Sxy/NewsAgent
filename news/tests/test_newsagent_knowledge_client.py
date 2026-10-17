"""Crawler document identity contracts without HTTP or configured credentials."""

from crawler.tencent_news import NewsArticle
from service.newsagent_knowledge_client import NewsAgentKnowledgeClient


def document(news_id):
    article = NewsArticle(
        url=f"https://news.qq.com/rain/a/{news_id}",
        title="新闻标题", publish_time="2026-10-12T12:00:00+08:00",
        author="新闻来源", content="新闻正文",
    )
    client = NewsAgentKnowledgeClient.__new__(NewsAgentKnowledgeClient)
    return client._document(article, article.content, None)


def test_short_document_identity_remains_compatible():
    assert document("news-one")["document_id"] == "news-news-one"
    assert document("x" * 155)["document_id"] == "news-" + "x" * 155


def test_long_news_ids_do_not_overwrite_one_another():
    first = document("x" * 155 + "a")
    second = document("x" * 155 + "b")
    assert first["document_id"] != second["document_id"]
    assert first["document_id"] == document("x" * 155 + "a")["document_id"]
    assert len(first["document_id"]) <= 160
    assert first["news_id"] == "x" * 155 + "a"
    assert second["news_id"] == "x" * 155 + "b"
