import json

from scripts import generate_daily_report


def test_main_builds_and_writes_report(monkeypatch, capsys, tmp_path):
    captured = {}

    class FakeService:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def build(self, target_date, **kwargs):
            captured["build"] = (target_date, kwargs)
            return {"status": "success", "target_date": target_date}

        def write(self, report, output_dir):
            captured["write"] = (report, output_dir)
            return {"json": "report.json", "markdown": "report.md"}

    monkeypatch.setattr(generate_daily_report, "DailyReportService", FakeService)
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_daily_report.py",
            "--date", "2026-08-20",
            "--output-dir", str(tmp_path),
            "--ingest-exit-code", "0",
            "--qa-exit-code", "1",
            "--evaluation-exit-code", "0",
        ],
    )

    assert generate_daily_report.main() == 0
    assert captured["build"] == (
        "2026-08-20",
        {
            "ingest_exit_code": 0,
            "qa_exit_code": 1,
            "evaluation_exit_code": 0,
        },
    )
    assert json.loads(capsys.readouterr().out)["files"]["json"] == "report.json"


def test_main_returns_one_on_error(monkeypatch, capsys):
    class FailingService:
        def __init__(self, **kwargs):
            raise RuntimeError("模拟报告错误")

    monkeypatch.setattr(generate_daily_report, "DailyReportService", FailingService)
    monkeypatch.setattr("sys.argv", ["generate_daily_report.py", "--date", "2026-08-20"])
    assert generate_daily_report.main() == 1
    assert "模拟报告错误" in capsys.readouterr().err
