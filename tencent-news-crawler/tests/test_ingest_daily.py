import json

from scripts import ingest_daily


def test_get_today_returns_iso_date():
    value = ingest_daily.get_today("Asia/Shanghai")
    assert len(value) == 10
    assert value[4] == "-"
    assert value[7] == "-"


def test_failure_helpers():
    assert ingest_daily.has_source_failure({
        "sources": [{"status": "failed"}]
    })
    assert not ingest_daily.has_source_failure({
        "sources": [{"status": "success"}]
    })
    assert ingest_daily.has_ingest_failure({
        "ingest": {"failed": 1}
    })
    assert not ingest_daily.has_ingest_failure({"ingest": None})


def test_main_passes_daily_arguments(monkeypatch, capsys):
    captured = {}

    class FakeService:
        def discover_and_ingest(self, **kwargs):
            captured.update(kwargs)
            return {
                "sources": [{"status": "success"}],
                "ingest": None,
                "items": [],
            }

    monkeypatch.setattr(ingest_daily, "DiscoveryIngestService", FakeService)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ingest_daily.py",
            "--date",
            "2026-08-19",
            "--limit-per-topic",
            "5",
            "--max-pages",
            "2",
            "--dry-run",
        ],
    )

    exit_code = ingest_daily.main()

    assert exit_code == 0
    assert captured == {
        "sources": ingest_daily.DEFAULT_SOURCES,
        "limit_per_topic": 5,
        "target_date": "2026-08-19",
        "max_pages": 2,
        "dry_run": True,
    }
    assert json.loads(capsys.readouterr().out)["status"] == "success"


def test_main_uses_default_date(monkeypatch, capsys):
    captured = {}

    class FakeService:
        def discover_and_ingest(self, **kwargs):
            captured.update(kwargs)
            return {"sources": [], "ingest": None}

    monkeypatch.setattr(ingest_daily, "DiscoveryIngestService", FakeService)
    monkeypatch.setattr(ingest_daily, "get_today", lambda timezone: "2026-08-20")
    monkeypatch.setattr("sys.argv", ["ingest_daily.py", "--dry-run"])

    assert ingest_daily.main() == 0
    assert captured["target_date"] == "2026-08-20"
    capsys.readouterr()


def test_main_returns_one_for_partial_failure(monkeypatch, capsys):
    class FakeService:
        def discover_and_ingest(self, **kwargs):
            return {
                "sources": [{"status": "success"}],
                "ingest": {"failed": 1},
            }

    monkeypatch.setattr(ingest_daily, "DiscoveryIngestService", FakeService)
    monkeypatch.setattr(
        "sys.argv",
        ["ingest_daily.py", "--date", "2026-08-19"],
    )

    assert ingest_daily.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "partial_failed"


def test_main_reports_exception_as_json(monkeypatch, capsys):
    class FailingService:
        def discover_and_ingest(self, **kwargs):
            raise RuntimeError("模拟异常")

    monkeypatch.setattr(ingest_daily, "DiscoveryIngestService", FailingService)
    monkeypatch.setattr(
        "sys.argv",
        ["ingest_daily.py", "--date", "2026-08-19"],
    )

    assert ingest_daily.main() == 1
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "failed"
    assert error["error"] == "模拟异常"
