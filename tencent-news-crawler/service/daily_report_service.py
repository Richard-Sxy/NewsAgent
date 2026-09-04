import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class DailyReportService:
    """根据本地任务数据库生成每日新闻与 QA 统计。"""

    def __init__(
        self,
        db_path: str,
        article_cache_dir: str,
        timezone_name: str = "Asia/Shanghai",
        ingest_max_retry_count: int = 3,
        qa_max_retry_count: int = 3,
    ) -> None:
        self.db_path = db_path
        self.article_cache_dir = Path(article_cache_dir)
        self.timezone = ZoneInfo(timezone_name)
        self.ingest_max_retry_count = ingest_max_retry_count
        self.qa_max_retry_count = qa_max_retry_count

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _status_counts(rows: list[sqlite3.Row]) -> dict[str, int]:
        counts = Counter(row["status"] for row in rows)
        return dict(sorted(counts.items()))

    def build(
        self,
        target_date: str,
        ingest_exit_code: int | None = None,
        qa_exit_code: int | None = None,
        evaluation_exit_code: int | None = None,
    ) -> dict:
        datetime.strptime(target_date, "%Y-%m-%d")

        news_rows: list[sqlite3.Row] = []
        qa_rows: list[sqlite3.Row] = []
        with self._connect() as connection:
            if self._table_exists(connection, "news_ingest_records"):
                news_rows = connection.execute(
                    """
                    SELECT url, status, retry_count
                    FROM news_ingest_records
                    WHERE substr(publish_time, 1, 10) = ?
                    """,
                    (target_date,),
                ).fetchall()

            if self._table_exists(connection, "news_qa_records"):
                qa_rows = connection.execute(
                    """
                    SELECT qa.url, qa.status, qa.retry_count
                    FROM news_qa_records AS qa
                    INNER JOIN news_ingest_records AS news
                        ON news.url = qa.url
                    WHERE substr(news.publish_time, 1, 10) = ?
                    """,
                    (target_date,),
                ).fetchall()

        successful_news_urls = {
            row["url"] for row in news_rows if row["status"] == "success"
        }
        qa_urls = {row["url"] for row in qa_rows}
        cache_count = (
            sum(1 for path in self.article_cache_dir.glob("*.json") if path.is_file())
            if self.article_cache_dir.exists()
            else 0
        )

        pipeline_codes = {
            "ingest": ingest_exit_code,
            "qa": qa_exit_code,
            "evaluation": evaluation_exit_code,
        }
        known_codes = [code for code in pipeline_codes.values() if code is not None]
        pipeline_status = (
            "failed" if any(code != 0 for code in known_codes) else "success"
        )
        if not known_codes:
            pipeline_status = "unknown"

        return {
            "status": pipeline_status,
            "target_date": target_date,
            "generated_at": datetime.now(self.timezone).isoformat(),
            "pipeline_exit_codes": pipeline_codes,
            "news": {
                "total": len(news_rows),
                "status_counts": self._status_counts(news_rows),
                "retry_exhausted": sum(
                    row["status"] == "failed"
                    and row["retry_count"] >= self.ingest_max_retry_count
                    for row in news_rows
                ),
            },
            "qa": {
                "total": len(qa_rows),
                "status_counts": self._status_counts(qa_rows),
                "not_submitted": len(successful_news_urls - qa_urls),
                "retry_exhausted": sum(
                    row["status"] == "failed"
                    and row["retry_count"] >= self.qa_max_retry_count
                    for row in qa_rows
                ),
            },
            "cache": {"article_files": cache_count},
        }
    """转成Markdown格式文件"""
    @staticmethod
    def to_markdown(report: dict) -> str:
        def statuses(value: dict) -> str:
            counts = value.get("status_counts", {})
            return "、".join(f"{key}: {count}" for key, count in counts.items()) or "无"

        return "\n".join([
            f"# 新闻每日任务报告（{report['target_date']}）",
            "",
            f"- 任务状态：{report['status']}",
            f"- 生成时间：{report['generated_at']}",
            f"- 新闻任务退出码：{report['pipeline_exit_codes']['ingest']}",
            f"- QA 任务退出码：{report['pipeline_exit_codes']['qa']}",
            f"- 自动评测退出码：{report['pipeline_exit_codes']['evaluation']}",
            f"- 当日新闻总数：{report['news']['total']}",
            f"- 新闻状态：{statuses(report['news'])}",
            f"- 新闻重试耗尽：{report['news']['retry_exhausted']}",
            f"- 当日 QA 记录：{report['qa']['total']}",
            f"- QA 状态：{statuses(report['qa'])}",
            f"- 尚未提交 QA：{report['qa']['not_submitted']}",
            f"- QA 重试耗尽：{report['qa']['retry_exhausted']}",
            f"- 正文缓存文件：{report['cache']['article_files']}",
            "",
        ])
    """写入到内容当中"""
    def write(self, report: dict, output_dir: str) -> dict[str, str]:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"daily-{report['target_date']}"
        json_path = directory / f"{stem}.json"
        markdown_path = directory / f"{stem}.md"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(self.to_markdown(report), encoding="utf-8")
        return {"json": str(json_path), "markdown": str(markdown_path)}
