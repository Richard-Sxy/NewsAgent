import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

from intelligence.title_nlp import TitleAnalysis

""" SQLite存储服务 """
class IngestRepository:

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.initialize()
    # 建立连接函数
    def connect(self):
        return sqlite3.connect(self.db_path)
    # 初始化表格
    def initialize(self):
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS news_ingest_records(
                    url TEXT PRIMARY KEY,
                    title TEXT,
                    collection_id TEXT,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    topic TEXT,
                    source_title TEXT,
                    publish_time TEXT,
                    discovered_at TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS news_title_analyses(
                    url TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    tokens_json TEXT NOT NULL,
                    extractor TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    analyzed_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS news_title_entities(
                    url TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    start_char INTEGER NOT NULL,
                    end_char INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (
                        url, entity_type, normalized_text, start_char, end_char
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_title_entities_lookup
                ON news_title_entities(entity_type, normalized_text)
                """
            )

        self.migrate()

    """做原来的表格的迁移,固定字段做添加"""
    def migrate(self) -> None:
        columns = {
            "topic": "TEXT",
            "source_title": "TEXT",
            "publish_time": "TEXT",
            "discovered_at": "TEXT",
            "retry_count": "INTEGER NOT NULL DEFAULT 0",
            "last_attempt_at": "TEXT",
        }

        with self.connect() as connection:
            existing_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(news_ingest_records)"
                ).fetchall()
            }

            for column_name, column_type in columns.items():
                if column_name in existing_columns:
                    continue

                connection.execute(
                    f"""
                    ALTER TABLE news_ingest_records
                    ADD COLUMN {column_name} {column_type}
                    """
                )

    """ 记录自动发现的新闻 """
    def mark_discovered(
        self,
        url: str,
        source_title: str,
        topic: str,
        publish_time: str,
    ) -> None:
        now = datetime.now(
            timezone.utc
        ).isoformat()

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO news_ingest_records (
                    url,
                    title,
                    status,
                    topic,
                    source_title,
                    publish_time,
                    discovered_at,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?,
                    ?,
                    'discovered',
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?
                )
                ON CONFLICT(url) DO UPDATE SET
                    topic = excluded.topic,
                    source_title = excluded.source_title,
                    publish_time = excluded.publish_time,
                    updated_at = excluded.updated_at
                """,
                (
                    url,
                    source_title,
                    topic,
                    source_title,
                    publish_time,
                    now,
                    now,
                    now,
                ),
            )

    def mark_discovered_many(
        self,
        records: list[dict],
    ) -> None:
        """在单个事务中登记大量发现记录，避免逐条建连带来的开销。"""
        if not records:
            return
        now = datetime.now(timezone.utc).isoformat()
        values = [
            (
                record["url"],
                record.get("source_title", ""),
                record.get("topic", ""),
                record.get("source_title", ""),
                record.get("publish_time", ""),
                now,
                now,
                now,
            )
            for record in records
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO news_ingest_records (
                    url, title, status, topic, source_title, publish_time,
                    discovered_at, created_at, updated_at
                )
                VALUES (?, ?, 'discovered', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    topic = excluded.topic,
                    source_title = excluded.source_title,
                    publish_time = excluded.publish_time,
                    updated_at = excluded.updated_at
                """,
                values,
            )

    """ 根据URL查询记录 """
    def get_by_url(self, url: str):
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row

            row = connection.execute(
                """
                SELECT
                    url,
                    title,
                    collection_id,
                    status,
                    error_message,
                    topic,
                    source_title,
                    publish_time,
                    discovered_at,
                    retry_count,
                    last_attempt_at,
                    created_at,
                    updated_at
                FROM news_ingest_records
                WHERE url = ?
                """,
                (url,),
            ).fetchone()

            return dict(row) if row else None
    
    """ 标记正在入库 """
    def mark_pending(self, url: str):
        now = datetime.now(timezone.utc).isoformat()

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO news_ingest_records (
                    url,
                    status,
                    last_attempt_at,
                    created_at,
                    updated_at
                )
                VALUES (?, 'pending', ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    status = 'pending',
                    error_message = NULL,
                    last_attempt_at = excluded.updated_at,
                    updated_at = excluded.updated_at
                """,
                (url, now, now, now),
            )

    """ 标记成功 """
    def mark_success(
        self,
        url: str,
        title: str,
        collection_id: str,
    ):
        now = datetime.now(timezone.utc).isoformat()

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE news_ingest_records
                SET
                    title = ?,
                    collection_id = ?,
                    status = 'success',
                    error_message = NULL,
                    retry_count = 0,
                    updated_at = ?
                WHERE url = ?
                """,
                (
                    title,
                    collection_id,
                    now,
                    url,
                ),
            )

    def save_title_analysis(self, url: str, analysis: TitleAnalysis) -> None:
        """原子替换一篇新闻的标题分析和可查询实体行。"""
        now = datetime.now(timezone.utc).isoformat()
        tokens_json = json.dumps(
            [
                {
                    "text": token.text,
                    "pos": token.pos,
                    "start": token.start,
                    "end": token.end,
                }
                for token in analysis.tokens
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO news_title_analyses (
                    url, title, tokens_json, extractor, model_version,
                    status, error_message, analyzed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'success', NULL, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title = excluded.title,
                    tokens_json = excluded.tokens_json,
                    extractor = excluded.extractor,
                    model_version = excluded.model_version,
                    status = 'success',
                    error_message = NULL,
                    analyzed_at = excluded.analyzed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    url,
                    analysis.title,
                    tokens_json,
                    analysis.extractor,
                    analysis.model_version,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM news_title_entities WHERE url = ?",
                (url,),
            )
            connection.executemany(
                """
                INSERT INTO news_title_entities (
                    url, entity_type, entity_text, normalized_text,
                    start_char, end_char, source, confidence, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        url,
                        entity.entity_type,
                        entity.text,
                        entity.normalized_text,
                        entity.start,
                        entity.end,
                        entity.source,
                        entity.confidence,
                        now,
                    )
                    for entity in analysis.entities
                ],
            )

    def mark_title_analysis_failed(
        self,
        url: str,
        title: str,
        error_message: str,
        *,
        extractor: str = "ltp",
        model_version: str = "unknown",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO news_title_analyses (
                    url, title, tokens_json, extractor, model_version,
                    status, error_message, analyzed_at, updated_at
                )
                VALUES (?, ?, '[]', ?, ?, 'failed', ?, NULL, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title = excluded.title,
                    tokens_json = '[]',
                    extractor = excluded.extractor,
                    model_version = excluded.model_version,
                    status = 'failed',
                    error_message = excluded.error_message,
                    analyzed_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (url, title, extractor, model_version, error_message[:2000], now),
            )
            connection.execute(
                "DELETE FROM news_title_entities WHERE url = ?",
                (url,),
            )

    def get_title_analysis(self, url: str) -> dict | None:
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            analysis_row = connection.execute(
                """
                SELECT url, title, tokens_json, extractor, model_version,
                       status, error_message, analyzed_at, updated_at
                FROM news_title_analyses
                WHERE url = ?
                """,
                (url,),
            ).fetchone()
            if analysis_row is None:
                return None
            entity_rows = connection.execute(
                """
                SELECT entity_type, entity_text, normalized_text,
                       start_char, end_char, source, confidence
                FROM news_title_entities
                WHERE url = ?
                ORDER BY start_char, end_char, entity_type
                """,
                (url,),
            ).fetchall()
        result = dict(analysis_row)
        result["tokens"] = json.loads(result.pop("tokens_json"))
        result["entities"] = [dict(row) for row in entity_rows]
        return result

    """ 增加失败和重试次数 """
    def list_pending_qa(
        self,
        limit: int = 20,
        max_retry_count: int = 3,
    ) -> list[dict]:
        if limit < 1:
            raise ValueError(
                "limit 必须大于 0。"
            )
        if max_retry_count < 1:
            raise ValueError(
                "max_retry_count 必须大于 0。"
            )
        
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row

            rows = connection.execute(
                """
                SELECT
                    news.url,
                    news.title,
                    news.topic,
                    news.publish_time
                FROM news_ingest_records AS news
                LEFT JOIN news_qa_records AS qa
                    ON qa.url = news.url
                WHERE news.status = 'success'
                    AND (
                        qa.url IS NULL
                        OR (
                            qa.status = 'failed'
                            AND qa.retry_count < ?
                        )
                    )
                ORDER BY
                    CASE
                        WHEN qa.status = 'failed'
                        THEN 0
                        ELSE 1
                    END,
                    news.updated_at DESC
                LIMIT ?
                """,
                (max_retry_count, limit),
            ).fetchall()
        
        return [
            dict(row)
            for row in rows
        ]

    """ 按照URL和错误信息设置失败标记 """
    def mark_failed(
        self,
        url: str,
        error_message: str,
    ):
        now = datetime.now(timezone.utc).isoformat()

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE news_ingest_records
                SET
                    status = 'failed',
                    error_message = ?,
                    retry_count = retry_count + 1,
                    last_attempt_at = ?,
                    updated_at = ?
                WHERE url = ?
                """,
                (
                    error_message,
                    now,
                    now,
                    url,
                ),
            )

    """ 重新设置重试次数 """
    def reset_retry(self, url: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE news_ingest_records
                SET
                    retry_count = 0,
                    error_message = NULL,
                    updated_at = ?
                WHERE url = ?
                """,
                (now, url),
            )

    """标记成功记录"""
    def list_success(self, limit: int = 10) -> list[dict]:
        if limit < 1:
            raise ValueError("limit 必须大于 0。")

        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT
                    url,
                    title,
                    topic,
                    publish_time
                FROM news_ingest_records
                WHERE status = 'success'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
