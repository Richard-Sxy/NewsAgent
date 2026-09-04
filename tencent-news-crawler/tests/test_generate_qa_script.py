import json

from scripts import generate_qa


class FakeService:
    result = None

    def generate_latest(self, limit):
        assert limit == 7
        return self.result

    def generate_for_url(self, url):
        return self.result


def test_main_returns_zero_when_batch_succeeds(monkeypatch, capsys):
    FakeService.result = {
        "total": 1,
        "submitted": 1,
        "skipped": 0,
        "failed": 0,
        "items": [],
    }
    monkeypatch.setattr(generate_qa, "NewsQAService", FakeService)
    monkeypatch.setattr("sys.argv", ["generate_qa", "--limit", "7"])

    assert generate_qa.main() == 0
    assert json.loads(capsys.readouterr().out)["submitted"] == 1


def test_main_returns_one_when_batch_has_failure(monkeypatch):
    FakeService.result = {
        "total": 1,
        "submitted": 0,
        "skipped": 0,
        "failed": 1,
        "items": [],
    }
    monkeypatch.setattr(generate_qa, "NewsQAService", FakeService)
    monkeypatch.setattr("sys.argv", ["generate_qa", "--limit", "7"])

    assert generate_qa.main() == 1


def test_main_returns_one_when_service_raises(monkeypatch, capsys):
    class FailingService:
        def generate_latest(self, limit):
            raise RuntimeError("模拟错误")

    monkeypatch.setattr(generate_qa, "NewsQAService", FailingService)
    monkeypatch.setattr("sys.argv", ["generate_qa", "--limit", "7"])

    assert generate_qa.main() == 1
    assert "模拟错误" in capsys.readouterr().err
