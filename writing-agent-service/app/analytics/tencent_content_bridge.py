"""把 ``tencent-news-crawler`` 的本地内容产物接入热点主链路。

这是本地/离线内容适配器，不是企业内容存储方案：它只读取爬虫写出的
JSON 正文缓存和 SQLite 入库台账，为热点富化提供精确正文，并把 FastGPT
``collection_id`` 关联回来。真实环境只需实现相同的
``NewsContentRepository`` 接口即可替换。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from app.analytics.news_content import TencentNewsCacheRepository


class TencentIngestContentRepository(TencentNewsCacheRepository):
    """合并爬虫 JSON 正文缓存与 SQLite 入库台账的内容仓储。

    ``cache_dir`` 对应 ``tencent-news-crawler/data/articles``，
    ``ingest_db_path`` 对应爬虫的 ``news_ingest_records`` SQLite 数据库。
    缺少台账时仍可提供正文，只是没有 ``collection_id``。
    """

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        ingest_db_path: str | Path | None = None,
    ) -> None:
        self._ingest_db_path = (
            Path(ingest_db_path) if ingest_db_path is not None else None
        )
        super().__init__(cache_dir)
        self._apply_collection_ids()

    def refresh(self) -> None:
        """重新扫描正文缓存和台账，用于接入新抓取的内容。"""

        self._contents = self._load_contents()
        self._apply_collection_ids()

    def _apply_collection_ids(self) -> None:
        collection_ids = self._load_collection_ids()
        if not collection_ids:
            return
        self._contents = {
            news_id: (
                content
                if content.collection_id
                or content.source_url not in collection_ids
                else replace(
                    content,
                    collection_id=collection_ids[content.source_url],
                )
            )
            for news_id, content in self._contents.items()
        }

    def _load_collection_ids(self) -> dict[str, str]:
        if self._ingest_db_path is None or not self._ingest_db_path.is_file():
            return {}
        try:
            connection = sqlite3.connect(
                f"file:{self._ingest_db_path}?mode=ro",
                uri=True,
            )
        except sqlite3.Error:
            return {}
        try:
            rows = connection.execute(
                """
                SELECT url, collection_id
                FROM news_ingest_records
                WHERE collection_id IS NOT NULL
                    AND collection_id <> ''
                    AND status = 'success'
                """
            ).fetchall()
        except sqlite3.Error:
            return {}
        finally:
            connection.close()
        return {str(url): str(collection_id) for url, collection_id in rows}
