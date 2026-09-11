import sqlite3

from service.ingest_repository import IngestRepository
from service.qa_repository import QARepository
from intelligence.title_nlp import (
    TitleAnalysis,
    TitleEntity,
    TitleToken,
)

def test_repository_records_success(tmp_path):
    db_path = tmp_path / "test.db"
    repository = IngestRepository(str(db_path))

    url = "https://news.qq.com/rain/a/test"

    assert repository.get_by_url(url) is None

    repository.mark_pending(url)

    pending = repository.get_by_url(url)
    assert pending["status"] == "pending"

    repository.mark_success(
        url=url,
        title="测试新闻",
        collection_id="collection-test",
    )

    success = repository.get_by_url(url)

    assert success["status"] == "success"
    assert success["title"] == "测试新闻"
    assert success["collection_id"] == (
        "collection-test"
    )
    assert success["error_message"] is None

def test_repository_records_failure(tmp_path):
    repository = IngestRepository(
        str(tmp_path / "test.db")
    )

    url = "https://news.qq.com/rain/a/test"

    repository.mark_pending(url)
    repository.mark_failed(
        url=url,
        error_message="模拟失败",
    )

    record = repository.get_by_url(url)

    assert record["status"] == "failed"
    assert record["error_message"] == "模拟失败"


def test_repository_saves_queryable_title_analysis(tmp_path):
    repository = IngestRepository(str(tmp_path / "test.db"))
    url = "https://news.qq.com/rain/a/NLP"
    repository.mark_pending(url)
    analysis = TitleAnalysis(
        title="雷军发布新品",
        tokens=(
            TitleToken("雷军", "nh", 0, 2),
            TitleToken("发布", "v", 2, 4),
            TitleToken("新品", "n", 4, 6),
        ),
        entities=(
            TitleEntity("雷军", "雷军", "person", 0, 2, "ner"),
        ),
        extractor="ltp",
        model_version="LTP/tiny@test",
    )

    repository.save_title_analysis(url, analysis)

    stored = repository.get_title_analysis(url)
    assert stored["status"] == "success"
    assert stored["tokens"][0] == {
        "text": "雷军", "pos": "nh", "start": 0, "end": 2
    }
    assert stored["entities"][0]["normalized_text"] == "雷军"
    assert stored["entities"][0]["entity_type"] == "person"


def test_repository_records_title_analysis_failure(tmp_path):
    repository = IngestRepository(str(tmp_path / "test.db"))
    url = "https://news.qq.com/rain/a/NLP-FAILED"
    repository.mark_pending(url)

    repository.mark_title_analysis_failed(url, "标题", "模型不可用")

    stored = repository.get_title_analysis(url)
    assert stored["status"] == "failed"
    assert stored["error_message"] == "模型不可用"
    assert stored["entities"] == []


def test_repository_records_discovery_metadata(tmp_path):
    repository = IngestRepository(
        str(tmp_path / "test.db")
    )
    url = "https://news.qq.com/rain/a/DISCOVERED"

    repository.mark_discovered(
        url=url,
        source_title="分类页标题",
        topic="科技",
        publish_time="2026-08-19 10:00:00",
    )

    record = repository.get_by_url(url)
    assert record["status"] == "discovered"
    assert record["title"] == "分类页标题"
    assert record["source_title"] == "分类页标题"
    assert record["topic"] == "科技"
    assert record["publish_time"] == "2026-08-19 10:00:00"
    assert record["discovered_at"]


def test_repeated_discovery_preserves_success_and_first_time(tmp_path):
    repository = IngestRepository(
        str(tmp_path / "test.db")
    )
    url = "https://news.qq.com/rain/a/SUCCESS"
    repository.mark_discovered(url, "旧标题", "科技", "2026-08-19")
    first_discovered_at = repository.get_by_url(url)["discovered_at"]
    repository.mark_pending(url)
    repository.mark_success(url, "正文标题", "collection-1")

    repository.mark_discovered(url, "新标题", "体育", "2026-08-20")

    record = repository.get_by_url(url)
    assert record["status"] == "success"
    assert record["collection_id"] == "collection-1"
    assert record["title"] == "正文标题"
    assert record["source_title"] == "新标题"
    assert record["topic"] == "体育"
    assert record["publish_time"] == "2026-08-20"
    assert record["discovered_at"] == first_discovered_at


def test_repository_migrates_existing_database(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE news_ingest_records(
                url TEXT PRIMARY KEY,
                title TEXT,
                collection_id TEXT,
                status TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO news_ingest_records(
                url, status, created_at, updated_at
            ) VALUES (?, 'success', ?, ?)
            """,
            ("https://news.qq.com/rain/a/OLD", "before", "before"),
        )

    repository = IngestRepository(str(db_path))

    with repository.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(news_ingest_records)"
            )
        }

    assert {
        "topic",
        "source_title",
        "publish_time",
        "discovered_at",
        "retry_count",
        "last_attempt_at",
    }.issubset(columns)
    assert repository.get_by_url(
        "https://news.qq.com/rain/a/OLD"
    )["status"] == "success"


def test_list_pending_qa_selects_new_and_failed_but_excludes_submitted(tmp_path):
    db_path = str(tmp_path / "test.db")
    repository = IngestRepository(db_path)
    qa_repository = QARepository(db_path)

    new_url = "https://news.qq.com/rain/a/NEW"
    failed_url = "https://news.qq.com/rain/a/QA_FAILED"
    submitted_url = "https://news.qq.com/rain/a/QA_SUBMITTED"
    ingest_failed_url = "https://news.qq.com/rain/a/INGEST_FAILED"

    for url in (new_url, failed_url, submitted_url, ingest_failed_url):
        repository.mark_pending(url)
        if url == ingest_failed_url:
            repository.mark_failed(url, "抓取失败")
        else:
            repository.mark_success(url, url.rsplit("/", 1)[-1], "raw-id")

    qa_repository.mark_pending(failed_url)
    qa_repository.mark_failed(failed_url, "QA 生成失败")
    qa_repository.mark_pending(submitted_url)
    qa_repository.mark_submitted(submitted_url, "qa-id")

    records = repository.list_pending_qa(limit=20)

    assert [record["url"] for record in records] == [failed_url, new_url]


def test_list_pending_qa_applies_limit_and_validates_it(tmp_path):
    db_path = str(tmp_path / "test.db")
    repository = IngestRepository(db_path)
    QARepository(db_path)

    for index in range(3):
        url = f"https://news.qq.com/rain/a/NEW{index}"
        repository.mark_pending(url)
        repository.mark_success(url, f"新闻 {index}", f"raw-{index}")

    assert len(repository.list_pending_qa(limit=2)) == 2

    import pytest

    with pytest.raises(ValueError, match="limit 必须大于 0"):
        repository.list_pending_qa(limit=0)


def test_list_pending_qa_excludes_exhausted_failures(tmp_path):
    db_path = str(tmp_path / "test.db")
    repository = IngestRepository(db_path)
    qa_repository = QARepository(db_path)
    url = "https://news.qq.com/rain/a/EXHAUSTED"
    repository.mark_pending(url)
    repository.mark_success(url, "测试新闻", "raw-id")
    qa_repository.mark_pending(url)

    for index in range(3):
        qa_repository.mark_failed(url, f"失败 {index}")
        if index < 2:
            qa_repository.mark_pending(url)

    assert repository.list_pending_qa(
        limit=20,
        max_retry_count=3,
    ) == []

    qa_repository.reset_retry(url)
    assert repository.list_pending_qa(
        limit=20,
        max_retry_count=3,
    )[0]["url"] == url
