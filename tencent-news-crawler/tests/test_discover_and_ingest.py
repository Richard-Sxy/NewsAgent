import json

import pytest

from scripts import discover_and_ingest
from scripts.discover_and_ingest import parse_sources


def test_parse_sources():
    assert parse_sources([
        "科技=https://news.qq.com/ch/tech/",
        "体育=https://news.qq.com/ch/sports/",
    ]) == {
        "科技": "https://news.qq.com/ch/tech/",
        "体育": "https://news.qq.com/ch/sports/",
    }


@pytest.mark.parametrize("value", ["科技", "=url", "科技="])
def test_parse_sources_rejects_invalid_value(value):
    with pytest.raises(ValueError, match="来源格式错误"):
        parse_sources([value])


def test_parse_sources_rejects_duplicate_topic():
    with pytest.raises(ValueError, match="分类重复"):
        parse_sources([
            "科技=https://example.com/one",
            "科技=https://example.com/two",
        ])


def test_main_passes_target_date(monkeypatch, capsys):
    captured = {}

    class FakeDiscoveryIngestService:
        def discover_and_ingest(self, **kwargs):
            captured.update(kwargs)
            return {"discovered": 0, "items": []}

    monkeypatch.setattr(
        discover_and_ingest,
        "DiscoveryIngestService",
        FakeDiscoveryIngestService,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "discover_and_ingest.py",
            "--source",
            "科技=https://news.qq.com/ch/tech/",
            "--date",
            "2026-08-19",
            "--dry-run",
            "--max-pages",
            "5",
        ],
    )

    discover_and_ingest.main()

    assert captured["target_date"] == "2026-08-19"
    assert captured["max_pages"] == 5
    assert captured["dry_run"] is True
    assert json.loads(capsys.readouterr().out)["discovered"] == 0


def test_main_rejects_invalid_date(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "discover_and_ingest.py",
            "--source",
            "科技=https://news.qq.com/ch/tech/",
            "--date",
            "2026-99-99",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        discover_and_ingest.main()

    assert exc_info.value.code == 2
    assert "--date 必须使用 YYYY-MM-DD 格式" in (
        capsys.readouterr().err
    )
