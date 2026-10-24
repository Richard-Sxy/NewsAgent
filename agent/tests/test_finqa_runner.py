"""Offline FinQA runner contracts; synthetic fixtures and HTTP transport only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from evaluation import run_finqa as runner


PROGRAM = ["subtract(", "100", "80", ")", "EOF"]
PRIVATE_MARKER = "PRIVATE_GOLD_MUST_NOT_REACH_MODEL"


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def source_case(index: int) -> dict:
    return {
        "id": f"synthetic/report-{index}",
        "pre_text": ["Revenue was 100 in 2024 and 80 in 2023."],
        "table": [["Year", "2024", "2023"], ["Revenue", "100", "80"]],
        "post_text": ["Values are in millions."],
        "qa": {
            "question": "What is the increase in revenue in millions?",
            "program": "subtract(100, 80)",
            "exe_ans": 20,
            "answer": PRIVATE_MARKER,
            "program_re": PRIVATE_MARKER,
            "gold_inds": {"text_0": PRIVATE_MARKER},
            "model_input": PRIVATE_MARKER,
        },
        "model_input": PRIVATE_MARKER,
        "text_retrieved": [{"text": PRIVATE_MARKER}],
        "table_retrieved": [{"table": PRIVATE_MARKER}],
        "retriever": {"gold": PRIVATE_MARKER},
    }


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    finqa = tmp_path / "finqa"
    (finqa / "dataset").mkdir(parents=True)
    (finqa / "code/evaluate").mkdir(parents=True)
    cases = [source_case(index) for index in range(3)]
    write_json(finqa / "dataset/dev.json", cases)
    # These contracts must never run a substitute scorer and claim real accuracy.
    (finqa / "code/evaluate/evaluate.py").write_text(
        "raise AssertionError('fixture scorer must not execute')\n", encoding="utf-8"
    )
    monkeypatch.setattr(runner, "source_identity", lambda: {"fixture-source": "v1"})
    monkeypatch.setattr(runner, "dependencies", lambda: {"fixture-dependency": "v1"})
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **kw: "fixture-head\n")
    return SimpleNamespace(
        finqa=finqa,
        split="dev",
        limit=3,
        model="deepseek-flash",
        prompt_file=None,
        output_dir=tmp_path / "run",
    )


def response(content: str | None = None, *, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "mock-request-id"},
        json={
            "id": "mock-completion",
            "model": "deepseek-flash",
            "choices": [{"message": {"content": content if content is not None else
                                      json.dumps({"predicted": PROGRAM})},
                         "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
        },
    )


def records(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "cases.jsonl").read_text().splitlines()]


def test_prepare_only_exposes_positive_input_whitelist(package) -> None:
    spec = runner.prepare(package)
    inputs = runner.read(package.output_dir / "inputs.json")
    assert inputs[0]["input"] == {
        "pre_text": source_case(0)["pre_text"],
        "table": source_case(0)["table"],
        "post_text": source_case(0)["post_text"],
        "question": source_case(0)["qa"]["question"],
    }
    assert PRIVATE_MARKER not in json.dumps(inputs)
    assert "qa" not in inputs[0]["input"]
    assert spec["case_ids"] == [source_case(i)["id"] for i in range(3)]
    assert spec["planned"] == spec["budget"]["max_calls"] == 3
    assert spec["budget"]["retries"] == 0
    assert runner.verify(package.output_dir) == spec
    gold = runner.read(package.output_dir / "gold.json")
    assert set(gold[0]["qa"]) == {"program", "exe_ans"}


def test_prepare_rejects_duplicate_source_ids_even_outside_selected_subset(package) -> None:
    package.limit = 1
    cases = [source_case(0), source_case(1), source_case(1)]
    write_json(package.finqa / "dataset/dev.json", cases)
    with pytest.raises(ValueError, match="unique"):
        runner.prepare(package)
    assert not package.output_dir.exists()


@pytest.mark.parametrize("filename", ["inputs.json", "gold.json", "prompt.txt", "official_evaluate.py"])
def test_verify_rejects_frozen_artifact_drift(package, filename: str) -> None:
    runner.prepare(package)
    with (package.output_dir / filename).open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="frozen artifact changed"):
        runner.verify(package.output_dir)


def test_verify_rejects_original_dataset_drift(package) -> None:
    runner.prepare(package)
    with (package.finqa / "dataset/dev.json").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="original dataset changed"):
        runner.verify(package.output_dir)


def test_verify_rejects_run_spec_drift(package) -> None:
    runner.prepare(package)
    path = package.output_dir / "run-spec.json"
    spec = runner.read(path)
    spec["inference"]["model"] = "changed-model"
    write_json(path, spec)
    with pytest.raises(ValueError, match="run spec changed"):
        runner.verify(package.output_dir)


@pytest.mark.parametrize("function,message", [
    ("dependencies", "dependencies changed"),
    ("source_identity", "runner/runtime source"),
])
def test_verify_rejects_runtime_drift(package, monkeypatch, function: str, message: str) -> None:
    runner.prepare(package)
    monkeypatch.setattr(runner, function, lambda: {"changed": "v2"})
    with pytest.raises(ValueError, match=message):
        runner.verify(package.output_dir)


@pytest.mark.asyncio
async def test_real_http_port_sends_frozen_whitelisted_payload_and_records_usage(package) -> None:
    spec = runner.prepare(package)
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert str(request.url) == runner.ENDPOINT
        assert request.method == "POST"
        assert payload["model"] == "deepseek-flash"
        assert payload["temperature"] == 0
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["stream"] is False
        assert payload["max_tokens"] == 2048
        assert payload["messages"][0] == {"role": "system", "content": runner.PROMPT}
        item = json.loads(payload["messages"][1]["content"])
        assert set(item) == {"pre_text", "table", "post_text", "question"}
        assert PRIVATE_MARKER not in request.content.decode()
        assert request.headers["authorization"] == "Bearer fixture-api-key"
        assert request.headers["idempotency-key"] == f"{spec['run_id']}:{len(requests) - 1}"
        return response()

    report = await runner.execute(
        package.output_dir, "fixture-api-key", transport=httpx.MockTransport(handle)
    )
    assert len(requests) == 3
    assert report["status"] == "completed"
    assert report["counts"] == {"ok": 3}
    assert report["planned"] == report["attempted"] == 3
    assert runner.read(package.output_dir / "predictions.json") == [
        {"id": case_id, "predicted": PROGRAM} for case_id in spec["case_ids"]
    ]
    assert records(package.output_dir)[0]["usage"]["total_tokens"] == 50
    assert records(package.output_dir)[0]["request_id"] == "mock-request-id"
    assert runner.read(package.output_dir / "responses/0000.json")["model"] == "deepseek-flash"
    assert runner.read(package.output_dir / "execution-started.json")["environment"] == "mock_transport"
    for path in package.output_dir.rglob("*"):
        if path.is_file():
            assert "fixture-api-key" not in path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,kind", [(429, "rate_limit"), (503, "http_error")])
async def test_http_failure_stops_without_retry_and_retains_denominator(package, monkeypatch, status, kind) -> None:
    spec = runner.prepare(package)
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return response()
        return httpx.Response(status, json={"error": "synthetic failure"}, headers={"Retry-After": "1"})

    report = await runner.execute(package.output_dir, "fixture-key", transport=httpx.MockTransport(handle))
    assert len(requests) == 2
    assert report["planned"] == 3
    assert report["attempted"] == 2
    assert report["status"] == "blocked"
    assert report["stop_reason"] == kind
    assert report["counts"] == {"ok": 1, "request_failed": 1, "not_run": 1}
    predictions = runner.read(package.output_dir / "predictions.json")
    assert [p["id"] for p in predictions] == spec["case_ids"]
    assert predictions[1]["predicted"] == predictions[2]["predicted"] == ["EOF"]
    assert records(package.output_dir)[2]["stop_reason"] == kind

    def forbidden_scorer(*args, **kwargs):
        pytest.fail("infrastructure-blocked batch must not run scorer")

    monkeypatch.setattr(runner.subprocess, "run", forbidden_scorer)
    scored = runner.score(package.output_dir)
    assert scored["status"] == "blocked"
    assert scored["planned"] == 3
    assert scored["execution_accuracy"] is None
    assert scored["program_accuracy"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("content,finish_reason", [
    ("not JSON", "stop"),
    (json.dumps({"predicted": PROGRAM, "answer": 20}), "stop"),
    (json.dumps({"predicted": ["subtract(", "#0", "80", ")", "EOF"]}), "stop"),
    (json.dumps({"predicted": PROGRAM}), "length"),
])
async def test_invalid_model_output_gets_eof_and_does_not_drop_later_cases(package, content, finish_reason) -> None:
    runner.prepare(package)
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return response(content, finish_reason=finish_reason) if count == 1 else response()

    report = await runner.execute(package.output_dir, "fixture-key", transport=httpx.MockTransport(handle))
    assert count == report["planned"] == report["attempted"] == 3
    assert report["status"] == "completed"
    assert report["counts"] == {"invalid_output": 1, "ok": 2}
    predictions = runner.read(package.output_dir / "predictions.json")
    assert predictions[0]["predicted"] == ["EOF"]
    assert predictions[1]["predicted"] == PROGRAM


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [{"content": ""}, {}], ids=["empty-content", "missing-content"])
@pytest.mark.parametrize("model_bound", [True, False], ids=["bound-model", "different-model"])
async def test_empty_or_missing_answer_requires_model_binding_to_continue(package, message, model_bound) -> None:
    spec = runner.prepare(package)
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if calls > 1:
            return response()
        body = response().json()
        body["model"] = "deepseek-flash" if model_bound else "unrequested-model"
        body["choices"][0]["message"] = message
        return httpx.Response(200, headers={"x-request-id": "empty-response-id"}, json=body)

    execution = await runner.execute(
        package.output_dir, "fixture-key", transport=httpx.MockTransport(handle)
    )
    predictions = runner.read(package.output_dir / "predictions.json")
    assert [row["id"] for row in predictions] == spec["case_ids"]
    assert predictions[0]["predicted"] == ["EOF"]
    assert execution["planned"] == 3
    first = records(package.output_dir)[0]
    assert first["request_id"] == "empty-response-id"
    if model_bound:
        assert calls == execution["attempted"] == 3
        assert execution["status"] == "completed"
        assert execution["counts"] == {"invalid_output": 1, "ok": 2}
        assert first["error"] == "empty_or_missing_model_content"
        assert first["model_version"] == "deepseek-flash"
        assert first["usage"]["total_tokens"] == 50
        assert predictions[1]["predicted"] == predictions[2]["predicted"] == PROGRAM
    else:
        assert calls == execution["attempted"] == 1
        assert execution["status"] == "blocked"
        assert execution["counts"] == {"request_failed": 1, "not_run": 2}
        assert execution["stop_reason"] == "response_validation"
        assert predictions[1]["predicted"] == predictions[2]["predicted"] == ["EOF"]
        assert all(row["status"] == "not_run" for row in records(package.output_dir)[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["insufficient_system_resource", "aborted"])
@pytest.mark.parametrize("content", ["", json.dumps({"predicted": PROGRAM})], ids=["empty", "valid-program"])
async def test_provider_interruption_blocks_batch_even_with_valid_content(package, monkeypatch, reason, content) -> None:
    spec = runner.prepare(package)
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return response(content, finish_reason=reason)

    execution = await runner.execute(
        package.output_dir, "fixture-key", transport=httpx.MockTransport(handle)
    )
    assert calls == execution["attempted"] == 1
    assert execution["planned"] == 3
    assert execution["status"] == "blocked"
    assert execution["counts"] == {"request_failed": 1, "not_run": 2}
    assert execution["stop_reason"] == reason
    assert runner.read(package.output_dir / "predictions.json") == [
        {"id": case_id, "predicted": ["EOF"]} for case_id in spec["case_ids"]
    ]
    assert [row["status"] for row in records(package.output_dir)] == ["request_failed", "not_run", "not_run"]

    def forbidden_scorer(*args, **kwargs):
        pytest.fail("provider interruption must block scoring")

    monkeypatch.setattr(runner.subprocess, "run", forbidden_scorer)
    result = runner.score(package.output_dir)
    assert result["status"] == "blocked"
    assert result["execution_accuracy"] is None
    assert result["program_accuracy"] is None


@pytest.mark.asyncio
async def test_same_run_cannot_make_duplicate_calls(package) -> None:
    runner.prepare(package)
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return response()

    transport = httpx.MockTransport(handle)
    await runner.execute(package.output_dir, "fixture-key", transport=transport)
    original = (package.output_dir / "predictions.json").read_bytes()
    with pytest.raises((FileExistsError, ValueError)):
        await runner.execute(package.output_dir, "fixture-key", transport=transport)
    assert count == 3
    assert (package.output_dir / "predictions.json").read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
async def test_score_rejects_incomplete_or_duplicate_ids_before_scorer(package, monkeypatch, mutation) -> None:
    runner.prepare(package)
    await runner.execute(package.output_dir, "fixture-key", transport=httpx.MockTransport(lambda r: response()))
    predictions_path = package.output_dir / "predictions.json"
    predictions = runner.read(predictions_path)
    if mutation == "missing":
        predictions.pop()
    else:
        predictions[-1] = predictions[0]
    write_json(predictions_path, predictions)
    # Recompute the execution hash to exercise coverage validation independently.
    execution = runner.read(package.output_dir / "execution.json")
    execution["predictions_sha256"] = runner.digest(predictions_path)
    write_json(package.output_dir / "execution.json", execution)

    def forbidden_scorer(*args, **kwargs):
        pytest.fail("invalid sample coverage must never reach scorer")

    monkeypatch.setattr(runner.subprocess, "run", forbidden_scorer)
    with pytest.raises(ValueError, match="cover the frozen set exactly once"):
        runner.score(package.output_dir)
    assert not (package.output_dir / "results.json").exists()


@pytest.mark.asyncio
async def test_official_scorer_counts_correct_wrong_and_invalid_predictions(package) -> None:
    official = runner.DEFAULT_FINQA / "code/evaluate/evaluate.py"
    if not official.is_file():
        pytest.skip("downloaded official FinQA scorer is absent; no substitute scorer used")
    for dependency in ("numpy", "tqdm", "sympy"):
        pytest.importorskip(dependency, reason=f"optional FinQA scorer dependency {dependency} is absent")
    official_bytes = official.read_bytes()
    (package.finqa / "code/evaluate/evaluate.py").write_bytes(official_bytes)
    runner.prepare(package)
    assert (package.output_dir / "official_evaluate.py").read_bytes() == official_bytes
    outputs = iter([
        json.dumps({"predicted": PROGRAM}),
        json.dumps({"predicted": ["add(", "100", "80", ")", "EOF"]}),
        "not JSON",
    ])
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return response(next(outputs))

    execution = await runner.execute(
        package.output_dir, "fixture-key", transport=httpx.MockTransport(handle)
    )
    assert calls == 3
    assert execution["counts"] == {"ok": 2, "invalid_output": 1}
    assert runner.read(package.output_dir / "predictions.json")[2]["predicted"] == ["EOF"]

    result = runner.score(package.output_dir)
    assert result["status"] == "completed"
    assert result["planned"] == 3
    assert result["environment"] == "mock_transport"
    assert result["execution_accuracy"] == pytest.approx(1 / 3)
    assert result["program_accuracy"] == pytest.approx(1 / 3)
    assert result["production_acceptance"] == "not_approved"
    assert runner.read(package.output_dir / "results.json") == result
    assert (package.output_dir / "scorer.log").is_file()

    original_result = (package.output_dir / "results.json").read_bytes()
    with pytest.raises(ValueError, match="results already exist"):
        runner.score(package.output_dir)
    assert (package.output_dir / "results.json").read_bytes() == original_result
