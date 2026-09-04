import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Callable

from crawler.tencent_news import NewsArticle


class ArticleCache:
    """ 将抓取后的新闻正文按URL缓存为本地JSON。 """

    def __init__(self, cache_dir: str | Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def path_for_url(self, url: str) -> Path:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    """验证新闻标题是否为空/新闻正文是否为空/新闻文本是否过短"""
    @staticmethod
    def validate(article: NewsArticle) -> None:
        if not article.title.strip():
            raise ValueError("新闻标题为空。")
        if not article.content.strip():
            raise ValueError("新闻正文为空。")
        if len(article.content.strip()) < 50:
            raise ValueError("新闻内容过短。")

    def load(self, url: str) -> NewsArticle | None:
        path = self.path_for_url(url)
        if not path.is_file():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("url") != url:
                return None
            article = NewsArticle(
                url=data["url"],
                title=data["title"],
                publish_time=data.get("publish_time", ""),
                author=data.get("author", ""),
                content=data["content"],
            )
            self.validate(article)
            return article
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def save(self, article: NewsArticle) -> Path:
        self.validate(article)
        path = self.path_for_url(article.url)
        payload = json.dumps(
            article.to_dict(),
            ensure_ascii=False,
            indent=2,
        )

        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.cache_dir,
                prefix=f".{path.stem}-",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_file.write(payload)
                temporary_path = Path(temporary_file.name)
            os.replace(temporary_path, path)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()
        return path

    def get_or_fetch(
        self,
        url: str,
        fetcher: Callable[[str], NewsArticle],
    ) -> tuple[NewsArticle, bool]:
        cached = self.load(url)
        if cached is not None:
            return cached, True

        article = fetcher(url)
        self.save(article)
        return article, False
