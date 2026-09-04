from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from intelligence.schemas import EventArticle


def article_id_from_url(url: str) -> str:
    value = urlparse(url).path.rstrip("/").split("/")[-1]
    return value or "unknown"


def load_metadata(db_path: str | Path) -> dict[str, dict]:
    path = Path(db_path)
    if not path.exists():
        return {}
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT url, topic, publish_time
            FROM news_ingest_records
            WHERE status = 'success'
            """
        ).fetchall()
    return {row["url"]: dict(row) for row in rows}

"""加载缓存文章"""
def load_cached_articles(
    cache_dir: str | Path,
    db_path: str | Path | None = None,
    limit: int | None = None,
) -> list[EventArticle]:
    if limit is not None and limit < 1:
        raise ValueError("limit 必须大于 0。")
    metadata = load_metadata(db_path) if db_path else {}
    restrict_to_success = db_path is not None and Path(db_path).exists()
    articles: list[EventArticle] = []
    seen_urls: set[str] = set()
    for path in sorted(Path(cache_dir).glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        url = str(payload.get("url", "")).strip()
        title = str(payload.get("title", "")).strip()
        content = str(payload.get("content", "")).strip()
        if not url or not title or not content or url in seen_urls:
            continue
        if restrict_to_success and url not in metadata:
            continue
        row = metadata.get(url, {})
        articles.append(EventArticle(
            article_id=article_id_from_url(url),
            url=url,
            title=title,
            content=content,
            publish_time=str(payload.get("publish_time") or row.get("publish_time") or ""),
            author=str(payload.get("author", "")),
            topic=str(row.get("topic") or ""),
        ))
        seen_urls.add(url)
        if limit is not None and len(articles) >= limit:
            break
    return articles
