import json
import sqlite3

from service.daily_report_service import DailyReportService


def create_database(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE news_ingest_records(
                url TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL,
                publish_time TEXT
            );
            CREATE TABLE news_qa_records(
                url TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO news_ingest_records VALUES (?, ?, ?, ?)",
            [
                ("u1", "success", 0, "2026-08-20 10:00:00"),
                ("u2", "failed", 3, "2026-08-20 11:00:00"),
                ("u3", "success", 0, "2026-08-19 10:00:00"),
            ],
        )
        connection.execute(
            "INSERT INTO news_qa_records VALUES (?, ?, ?)",
            ("u1", "submitted", 0),
        )


def test_build_counts_only_target_date(tmp_path):
    db_path = tmp_path / "records.db"
    cache_dir = tmp_path / "articles"
    cache_dir.mkdir()
    (cache_dir / "one.json").write_text("{}", encoding="utf-8")
    create_database(db_path)

    report = DailyReportService(
        str(db_path), str(cache_dir), ingest_max_retry_count=3
    ).build(
        "2026-08-20",
        ingest_exit_code=0,
        qa_exit_code=1,
        evaluation_exit_code=0,
    )

    assert report["status"] == "failed"
    assert report["news"] == {
        "total": 2,
        "status_counts": {"failed": 1, "success": 1},
        "retry_exhausted": 1,
    }
    assert report["qa"]["total"] == 1
    assert report["qa"]["not_submitted"] == 0
    assert report["cache"]["article_files"] == 1


def test_build_handles_database_without_tables(tmp_path):
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()
    report = DailyReportService(str(db_path), str(tmp_path / "missing")).build(
        "2026-08-20"
    )
    assert report["status"] == "unknown"
    assert report["news"]["total"] == 0
    assert report["qa"]["total"] == 0


def test_write_creates_json_and_markdown(tmp_path):
    service = DailyReportService(str(tmp_path / "empty.db"), str(tmp_path / "cache"))
    sqlite3.connect(tmp_path / "empty.db").close()
    report = service.build("2026-08-20", 0, 0, 0)
    paths = service.write(report, str(tmp_path / "reports"))

    saved = json.loads((tmp_path / "reports/daily-2026-08-20.json").read_text())
    markdown = (tmp_path / "reports/daily-2026-08-20.md").read_text()
    assert saved["target_date"] == "2026-08-20"
    assert "新闻每日任务报告（2026-08-20）" in markdown
    assert paths["json"].endswith("daily-2026-08-20.json")
