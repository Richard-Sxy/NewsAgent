import json

import pytest

from scripts import ingest_batch
from scripts.ingest_batch import load_urls


def test_load_urls_ignores_empty_lines_and_comments(tmp_path):
    file_path = tmp_path / "urls.txt"
    file_path.write_text(
        "\n"
        "# 测试新闻\n"
        "https://news.qq.com/rain/a/ARTICLE001\n"
        "  \n"
        "  https://news.qq.com/rain/a/ARTICLE002  \n",
        encoding="utf-8",
    )

    assert load_urls(str(file_path)) == [
        "https://news.qq.com/rain/a/ARTICLE001",
        "https://news.qq.com/rain/a/ARTICLE002",
    ]


def test_load_urls_rejects_missing_file(tmp_path):
    with pytest.raises(
        ValueError,
        match="URL 文件不存在",
    ):
        load_urls(str(tmp_path / "missing.txt"))


def test_main_imports_urls_and_prints_result(
    monkeypatch,
    tmp_path,
    capsys,
):
    file_path = tmp_path / "urls.txt"
    file_path.write_text(
        "https://news.qq.com/rain/a/ARTICLE001\n",
        encoding="utf-8",
    )
    captured = {}

    class FakeNewsIngestService:
        def ingest_urls(self, urls, max_items, workers=1):
            captured["urls"] = urls
            captured["max_items"] = max_items
            captured["workers"] = workers
            return {
                "total": 1,
                "succeeded": 1,
                "skipped": 0,
                "failed": 0,
                "items": [],
            }

    monkeypatch.setattr(
        ingest_batch,
        "NewsIngestService",
        FakeNewsIngestService,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "ingest_batch.py",
            str(file_path),
            "--max-items",
            "5",
        ],
    )

    ingest_batch.main()

    assert captured == {
        "urls": [
            "https://news.qq.com/rain/a/ARTICLE001"
        ],
        "max_items": 5,
        "workers": 1,
    }
    output = json.loads(capsys.readouterr().out)
    assert output["total"] == 1
    assert output["succeeded"] == 1


def test_main_reports_batch_error(
    monkeypatch,
    tmp_path,
    capsys,
):
    file_path = tmp_path / "urls.txt"
    file_path.write_text("\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        ["ingest_batch.py", str(file_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        ingest_batch.main()

    assert exc_info.value.code == 1
    assert "批量入库失败" in capsys.readouterr().err
