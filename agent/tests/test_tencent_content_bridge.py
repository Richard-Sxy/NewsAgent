"""本地爬虫内容桥接与确定性语料检索测试。"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.analytics.corpus_knowledge_search import CorpusKnowledgeSearchClient
from app.analytics.entities import ContentType
from app.analytics.news_content import InMemoryNewsContentRepository, NewsContent
from app.analytics.tencent_content_bridge import TencentIngestContentRepository
from app.clients.knowledge_base import RelatedNewsSearchQuery


NOW = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)


def write_article_cache(cache_dir: Path, news_id: str, title: str, body: str) -> str:
    url = f"https://news.qq.com/rain/a/{news_id}"
    payload = {
        "url": url,
        "title": title,
        "publish_time": NOW.isoformat(),
        "author": "记者",
        "content": body,
    }
    (cache_dir / f"{news_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return url


def write_ingest_db(db_path: Path, rows: list[tuple[str, str]]) -> None:
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE news_ingest_records(
            url TEXT PRIMARY KEY,
            title TEXT,
            collection_id TEXT,
            status TEXT NOT NULL
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO news_ingest_records(url, title, collection_id, status)
        VALUES (?, ?, ?, 'success')
        """,
        [(url, "标题", collection_id) for url, collection_id in rows],
    )
    connection.commit()
    connection.close()


def test_bridge_merges_cache_and_ingest_ledger(tmp_path: Path) -> None:
    cache_dir = tmp_path / "articles"
    cache_dir.mkdir()
    url = write_article_cache(cache_dir, "news-001", "新能源政策", "正文内容" * 20)
    db_path = tmp_path / "ingest.db"
    write_ingest_db(db_path, [(url, "collection-abc")])

    repository = TencentIngestContentRepository(
        cache_dir,
        ingest_db_path=db_path,
    )

    content = repository.get_by_news_id("news-001")
    assert content is not None
    assert content.collection_id == "collection-abc"
    assert content.title == "新能源政策"

    import asyncio

    batch = asyncio.run(
        repository.batch_get_by_news_ids(
            tenant_id="tenant-1",
            news_ids=("news-001", "missing"),
        )
    )
    assert set(batch) == {"news-001"}


def test_bridge_without_ledger_still_serves_content(tmp_path: Path) -> None:
    cache_dir = tmp_path / "articles"
    cache_dir.mkdir()
    write_article_cache(cache_dir, "news-002", "标题", "正文" * 30)

    repository = TencentIngestContentRepository(cache_dir)

    content = repository.get_by_news_id("news-002")
    assert content is not None
    assert content.collection_id is None


def test_bridge_refresh_picks_up_new_articles(tmp_path: Path) -> None:
    cache_dir = tmp_path / "articles"
    cache_dir.mkdir()
    write_article_cache(cache_dir, "news-001", "标题一", "正文" * 30)
    repository = TencentIngestContentRepository(cache_dir)
    assert repository.get_by_news_id("news-002") is None

    write_article_cache(cache_dir, "news-002", "标题二", "正文" * 30)
    repository.refresh()

    assert repository.get_by_news_id("news-002") is not None


def make_content(news_id: str, title: str, body: str) -> NewsContent:
    return NewsContent(
        news_id=news_id,
        title=title,
        summary=body[:50],
        content_type=ContentType.ARTICLE,
        publish_time=NOW,
        source_url=f"https://news.qq.com/rain/a/{news_id}",
        collection_id=f"collection-{news_id}",
        body=body,
    )


def test_corpus_search_ranks_by_overlap_and_excludes_source() -> None:
    repository = InMemoryNewsContentRepository(
        [
            make_content("n1", "新能源政策落地", "新能源 政策 汽车 补贴"),
            make_content("n2", "足球比赛结果", "足球 联赛 比分"),
            make_content("n3", "新能源产业观察", "新能源 产业 电池 政策"),
        ]
    )
    client = CorpusKnowledgeSearchClient(repository)
    query = RelatedNewsSearchQuery(
        query_id="q1",
        source_news_id="n1",
        title="新能源政策",
        summary="政策 补贴",
        candidate_limit=5,
    )

    import asyncio

    results = asyncio.run(
        client.batch_search_related_news((query,), tenant_id="tenant-1")
    )

    news_ids = [item.news_id for item in results["q1"]]
    assert "n1" not in news_ids
    assert "n2" not in news_ids
    assert news_ids == ["n3"]


def test_corpus_search_respects_candidate_limit() -> None:
    repository = InMemoryNewsContentRepository(
        [
            make_content("n1", "共同主题 事件", "共同主题 事件 细节一"),
            make_content("n2", "共同主题 事件", "共同主题 事件 细节二"),
            make_content("n3", "共同主题 事件", "共同主题 事件 细节三"),
        ]
    )
    client = CorpusKnowledgeSearchClient(repository)
    query = RelatedNewsSearchQuery(
        query_id="q1",
        source_news_id="n0",
        title="共同主题",
        summary="事件",
        candidate_limit=2,
    )

    import asyncio

    results = asyncio.run(
        client.batch_search_related_news((query,), tenant_id="tenant-1")
    )

    assert len(results["q1"]) == 2
    assert [item.news_id for item in results["q1"]] == ["n1", "n2"]
