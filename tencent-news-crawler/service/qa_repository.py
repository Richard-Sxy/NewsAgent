import sqlite3
from datetime import datetime, timezone
from pathlib import Path


"""记录新闻提交到 FastGPT QA 训练的状态。"""
class QARepository:

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        return sqlite3.connect(self.db_path)

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS news_qa_records(
                    url TEXT PRIMARY KEY,
                    qa_collection_id TEXT,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

        self.migrate()

    def migrate(self) -> None:
        columns = {
            "retry_count": "INTEGER NOT NULL DEFAULT 0",
            "last_attempt_at": "TEXT",
        }
        with self.connect() as connection:
            existing_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(news_qa_records)"
                ).fetchall()
            }
            for column_name, column_type in columns.items():
                if column_name in existing_columns:
                    continue
                connection.execute(
                    f"""
                    ALTER TABLE news_qa_records
                    ADD COLUMN {column_name} {column_type}
                    """
                )

    def get_by_url(self, url: str) -> dict | None:
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT
                    url,
                    qa_collection_id,
                    status,
                    error_message,
                    retry_count,
                    last_attempt_at,
                    created_at,
                    updated_at
                FROM news_qa_records
                WHERE url = ?
                """,
                (url,),
            ).fetchone()
        return dict(row) if row else None

    def mark_pending(self, url: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO news_qa_records(
                    url, status, last_attempt_at, created_at, updated_at
                ) VALUES (?, 'pending', ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    status = 'pending',
                    error_message = NULL,
                    last_attempt_at = excluded.updated_at,
                    updated_at = excluded.updated_at
                """,
                (url, now, now, now),
            )

    def mark_submitted(self, url: str, qa_collection_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE news_qa_records
                SET
                    qa_collection_id = ?,
                    status = 'submitted',
                    error_message = NULL,
                    retry_count = 0,
                    updated_at = ?
                WHERE url = ?
                """,
                (qa_collection_id, now, url),
            )

    def mark_failed(self, url: str, error_message: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE news_qa_records
                SET
                    status = 'failed',
                    error_message = ?,
                    retry_count = retry_count + 1,
                    last_attempt_at = ?,
                    updated_at = ?
                WHERE url = ?
                """,
                (error_message, now, now, url),
            )

    def reset_retry(self, url: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE news_qa_records
                SET
                    retry_count = 0,
                    error_message = NULL,
                    updated_at = ?
                WHERE url = ?
                """,
                (now, url),
            )
