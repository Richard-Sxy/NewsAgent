import json

import pytest

from app.analytics.entities import ContentType
from app.analytics.news_content import TencentNewsCacheRepository


def write_article(path, *, url: str, title: str = "测试新闻") -> None:
    path.write_text(
        json.dumps(
            {
                "url": url,
                "title": title,
                "publish_time": "2026-09-03 10:00:00",
                "author": "腾讯网",
                "content": "这是一段用于内容仓储测试的新闻正文。" * 20,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_cache_repository_gets_content_by_news_id(tmp_path) -> None:
    cache_dir = tmp_path / "articles"
    cache_dir.mkdir()
    write_article(
        cache_dir / "article.json",
        url="https://news.qq.com/rain/a/20260903A00100",
    )

    repository = TencentNewsCacheRepository(cache_dir)
    content = repository.get_by_news_id("20260903A00100")

    assert content is not None
    assert content.title == "测试新闻"
    assert content.content_type == ContentType.ARTICLE
    assert content.publish_time.utcoffset() is not None
    assert content.body.startswith("这是一段")
    assert repository.get_by_news_id("missing") is None


def test_cache_repository_rejects_duplicate_news_id(tmp_path) -> None:
    write_article(tmp_path / "one.json", url="https://news.qq.com/rain/a/same")
    write_article(tmp_path / "two.json", url="https://news.qq.com/rain/a/same?from=feed")

    with pytest.raises(ValueError, match="duplicate news_id"):
        TencentNewsCacheRepository(tmp_path)


def test_cache_repository_rejects_corrupt_file(tmp_path) -> None:
    (tmp_path / "bad.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid news cache file"):
        TencentNewsCacheRepository(tmp_path)
