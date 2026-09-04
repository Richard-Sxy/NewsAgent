import json

from scripts import check_training_status


def test_main_returns_zero_for_ready(monkeypatch, capsys):
    class ReadyService:
        def check_all(self):
            return {"status": "ready"}

    monkeypatch.setattr(
        check_training_status,
        "FastGPTTrainingService",
        ReadyService,
    )

    assert check_training_status.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_main_returns_two_while_training(monkeypatch):
    class TrainingService:
        def check_all(self):
            return {"status": "training"}

    monkeypatch.setattr(
        check_training_status,
        "FastGPTTrainingService",
        TrainingService,
    )

    assert check_training_status.main() == 2


def test_main_returns_one_for_error_or_exception(monkeypatch, capsys):
    class ErrorService:
        def check_all(self):
            return {"status": "error"}

    monkeypatch.setattr(
        check_training_status,
        "FastGPTTrainingService",
        ErrorService,
    )
    assert check_training_status.main() == 1

    class FailingService:
        def check_all(self):
            raise RuntimeError("连接失败")

    monkeypatch.setattr(
        check_training_status,
        "FastGPTTrainingService",
        FailingService,
    )
    assert check_training_status.main() == 1
    assert "连接失败" in capsys.readouterr().out
