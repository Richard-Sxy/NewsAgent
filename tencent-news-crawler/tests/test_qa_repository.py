from service.qa_repository import QARepository


def test_qa_repository_creates_table_and_records_submission(tmp_path):
    repository = QARepository(str(tmp_path / "test.db"))
    url = "https://news.qq.com/rain/a/QA001"

    repository.mark_pending(url)
    assert repository.get_by_url(url)["status"] == "pending"

    repository.mark_submitted(url, "collection-qa")
    record = repository.get_by_url(url)
    assert record["status"] == "submitted"
    assert record["qa_collection_id"] == "collection-qa"
    assert record["error_message"] is None


def test_qa_repository_records_failure(tmp_path):
    repository = QARepository(str(tmp_path / "test.db"))
    url = "https://news.qq.com/rain/a/QA002"
    repository.mark_pending(url)
    repository.mark_failed(url, "模拟失败")

    record = repository.get_by_url(url)
    assert record["status"] == "failed"
    assert record["error_message"] == "模拟失败"
    assert record["retry_count"] == 1
    assert record["last_attempt_at"]


def test_qa_repository_counts_failures_and_can_reset(tmp_path):
    repository = QARepository(str(tmp_path / "test.db"))
    url = "https://news.qq.com/rain/a/QA_RETRY"
    repository.mark_pending(url)

    for index in range(3):
        repository.mark_failed(url, f"失败 {index}")
        if index < 2:
            repository.mark_pending(url)

    assert repository.get_by_url(url)["retry_count"] == 3

    repository.reset_retry(url)
    record = repository.get_by_url(url)
    assert record["retry_count"] == 0
    assert record["error_message"] is None


def test_qa_repository_migrates_retry_columns(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE news_qa_records(
                url TEXT PRIMARY KEY,
                qa_collection_id TEXT,
                status TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    repository = QARepository(str(db_path))
    with repository.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(news_qa_records)"
            )
        }

    assert {"retry_count", "last_attempt_at"}.issubset(columns)
