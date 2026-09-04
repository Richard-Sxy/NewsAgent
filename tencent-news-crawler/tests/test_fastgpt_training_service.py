import httpx
import pytest

from service.fastgpt_training_service import FastGPTTrainingService


def make_response(data, status_code=200):
    return httpx.Response(
        status_code,
        json=data,
        request=httpx.Request("GET", "http://fastgpt.test/status"),
    )


def build_service(responses):
    pending = iter(responses)

    def fake_get(*args, **kwargs):
        return next(pending)

    service = FastGPTTrainingService(
        request_get=fake_get,
        raw_dataset_id="raw-dataset",
        qa_dataset_id="qa-dataset",
    )
    service.api_key = "test-key"
    return service


def status_responses(raw_queue, raw_error, qa_queue, qa_error):
    return [
        make_response({"code": 200, "data": raw_queue}),
        make_response({"code": 200, "data": {"hasError": raw_error}}),
        make_response({"code": 200, "data": qa_queue}),
        make_response({"code": 200, "data": {"hasError": qa_error}}),
    ]


def test_check_all_returns_ready():
    service = build_service(status_responses(
        {"trainingCount": 0, "rebuildingCount": 0}, False,
        {"trainingCount": 0, "rebuildingCount": 0}, False,
    ))

    result = service.check_all()

    assert result["status"] == "ready"
    assert result["raw_dataset"]["status"] == "ready"
    assert result["qa_dataset"]["status"] == "ready"


def test_check_all_returns_training_when_either_dataset_is_busy():
    service = build_service(status_responses(
        {"trainingCount": 2, "rebuildingCount": 0}, False,
        {"trainingCount": 0, "rebuildingCount": 0}, False,
    ))

    result = service.check_all()

    assert result["status"] == "training"
    assert result["raw_dataset"]["training_count"] == 2


def test_error_takes_precedence_over_training():
    service = build_service(status_responses(
        {"trainingCount": 3, "rebuildingCount": 0}, False,
        {"trainingCount": 0, "rebuildingCount": 0}, True,
    ))

    result = service.check_all()

    assert result["status"] == "error"
    assert result["qa_dataset"]["has_error"] is True


def test_get_rejects_http_error():
    service = build_service([make_response({}, status_code=500)])

    with pytest.raises(RuntimeError, match="FastGPT HTTP 500"):
        service.get_dataset_status("raw-dataset")


def test_get_rejects_business_error():
    service = build_service([
        make_response({"code": 500, "message": "模拟业务错误"}),
    ])

    with pytest.raises(RuntimeError, match="模拟业务错误"):
        service.get_dataset_status("raw-dataset")


def test_get_rejects_missing_dataset_id():
    service = build_service([])

    with pytest.raises(ValueError, match="datasetId 不能为空"):
        service.get_dataset_status("")
