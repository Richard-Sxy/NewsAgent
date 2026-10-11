"""Dialogue evaluator contracts: evidence checks, frozen fixtures and one submission."""

import copy
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from evaluation.run_hot_news_dialogue import (
    DEFAULT_DATASET, LOCAL_USER, DialogueDataset, Expectation, evaluate_turn, load_dataset,
    loopback_url, report_items, run_evaluation, summarize,
)


def successful_query():
    run_id = str(uuid4())
    start, end = "2026-10-09T19:00:00+08:00", "2026-10-09T20:00:00+08:00"
    intent = {"sort_by": "ctr", "sort_direction": "desc", "row_limit": 5, "content_type": "video"}
    item = {"rank": 1, "news_id": "synthetic-news-1", "title": "[合成热点样本]测试新闻",
            "metrics": {"content_type": "video", "window_start": start, "window_end": end,
                        "impressions": 100, "clicks": 10, "ctr": "0.1"},
            "hot_score": {"score": "0.5"}, "analysis": None, "baseline": None}
    detail = {"run": {"run_id": run_id, "status": "completed", "window_start": start, "window_end": end},
              "sql_tool_trace": {"query_id": "bound-query"}, "ranked_news": [item]}
    result = {"run_id": run_id, "workflow_id": "bound-workflow", "sql_query_id": "bound-query",
              "query_resolution": {"status": "ready", "intent": intent},
              "query_model_provider": "local", "window_start": start, "window_end": end,
              "parameters": {"window_start": start, "window_end": end, "row_limit": 5, "content_type": "video"},
              "items": report_items(detail)}
    result["sql"] = "SELECT news_id FROM dw.news_behavior_aggregate ORDER BY ctr DESC LIMIT :row_limit"
    detail["sql_tool_trace"]["preview"] = {"sql": result["sql"], "parameters": copy.deepcopy(result["parameters"])}
    turn = {"status": "completed", "error_code": None, "assistant_content": f"运行 {run_id} 查询 bound-query",
            "tools": [{"name": "query_hot_news", "status": "completed", "attempts": 1, "result": result}]}
    expected = Expectation(kind="query", allowed_tools=["query_hot_news"], intent=intent,
                           window_start=start, window_end=end)
    return expected, turn, {run_id: detail}


def test_frozen_dataset_has_thirty_cases_and_distinguishes_pending_target():
    dataset, digest = load_dataset()
    manifest = json.loads((DEFAULT_DATASET.parent / "manifest.json").read_text())
    assert manifest["sha256"] == digest
    assert len(dataset.cases) == manifest["case_count"] == 30
    assert sum(len(case.turns) for case in dataset.cases) == manifest["turn_count"] == 33
    assert sum(turn.expect.kind == "query" for case in dataset.cases for turn in case.turns) == 12
    assert [case.id for case in dataset.cases if case.support == "pending"] == ["date_followup_pending"]


@pytest.mark.parametrize("value", ["https://127.0.0.1:5175", "http://example.com:5175", "http://127.0.0.1",
                                   "http://127.0.0.1:5175/api", "http://token@127.0.0.1:5175",
                                   "http://127.0.0.1:5175?redirect=elsewhere", "http://127.0.0.1:5175#x"])
def test_remote_or_credentialled_endpoint_is_rejected(value):
    with pytest.raises(ValueError):
        loopback_url(value)


@pytest.mark.parametrize("value", ["http://127.0.0.1:5175", "http://localhost:8000", "http://[::1]:8000"])
def test_explicit_loopback_origin_is_allowed(value):
    assert loopback_url(value + "/") == value


def test_fixture_hash_change_is_detected(tmp_path):
    (tmp_path / "cases.json").write_bytes(DEFAULT_DATASET.read_bytes() + b" ")
    (tmp_path / "manifest.json").write_text((DEFAULT_DATASET.parent / "manifest.json").read_text())
    with pytest.raises(ValueError, match="SHA-256"):
        load_dataset(tmp_path / "cases.json")


@pytest.mark.parametrize("change", ["duplicate_id", "future_reference", "unfrozen_coverage", "out_of_range_query"])
def test_invalid_fixture_semantics_are_rejected(change):
    payload = json.loads(DEFAULT_DATASET.read_text())
    if change == "duplicate_id":
        payload["cases"][1]["id"] = payload["cases"][0]["id"]
    elif change == "future_reference":
        payload["cases"][0]["turns"][1]["expect"]["same_run_from_turn"] = 1
    elif change == "unfrozen_coverage":
        payload["coverage_end"] = "2026-10-11T00:00:00+08:00"
    else:
        payload["cases"][0]["turns"][0]["expect"].update(
            window_start="2026-10-10T00:00:00+08:00", window_end="2026-10-10T01:00:00+08:00")
    with pytest.raises(ValidationError):
        DialogueDataset.model_validate(payload)


def test_success_requires_authoritative_run_and_matching_sql_scope():
    dataset, _ = load_dataset()
    expected, turn, details = successful_query()
    assert evaluate_turn(expected, turn, dataset=dataset, run_details=details)["verdict"] == "passed"


def test_confirmed_sql_requires_understanding_provenance_and_no_second_model():
    dataset, _ = load_dataset()
    expected, turn, details = successful_query()
    result = turn["tools"][0]["result"]
    result.update(query_model_provider="python-confirmed-intent", query_model_request_id=None,
                  understanding_model_request_id="understanding-local-test")
    assert evaluate_turn(expected, turn, dataset=dataset, run_details=details)["verdict"] == "passed"
    result["query_model_request_id"] = "unexpected-second-model"
    assert "confirmed_intent_provenance_invalid" in evaluate_turn(expected, turn, dataset=dataset, run_details=details)["failures"]
    missing = evaluate_turn(expected, turn, dataset=dataset)
    assert "authoritative_run_not_verified" in missing["failures"]
    details[next(iter(details))]["sql_tool_trace"]["query_id"] = "wrong-query"
    assert "authoritative_run_query_or_snapshot_mismatch" in evaluate_turn(
        expected, turn, dataset=dataset, run_details=details)["failures"]


@pytest.mark.parametrize("change,code", [
    ("requested_window", "query_requested_window_changed"),
    ("sql_window", "sql_parameters_window_mismatch"),
    ("content_type", "ranked_item_content_type_mismatch"),
    ("intent", "query_intent_changed:sort_by"),
    ("duplicate_query", "query_submission_repeated"),
    ("extra_tool", "unnecessary_or_forbidden_tool"),
])
def test_invalid_query_cannot_pass_on_polite_assistant_text(change, code):
    dataset, _ = load_dataset()
    expected, turn, details = successful_query()
    result = turn["tools"][0]["result"]
    if change == "requested_window":
        result["window_start"] = "2026-10-09T18:00:00+08:00"
    elif change == "sql_window":
        result["parameters"]["window_start"] = "2026-10-09T18:00:00+08:00"
    elif change == "content_type":
        result["items"][0]["metrics"]["content_type"] = "article"
    elif change == "intent":
        result["query_resolution"]["intent"]["sort_by"] = "clicks"
    elif change == "duplicate_query":
        turn["tools"].append(copy.deepcopy(turn["tools"][0]))
    else:
        turn["tools"].append({"name": "search_knowledge", "status": "completed", "attempts": 1, "result": {}})
    assert code in evaluate_turn(expected, turn, dataset=dataset, run_details=details)["failures"]


def test_date_clarification_passes_only_without_query_binding_or_saved_result():
    dataset, _ = load_dataset()
    expected = Expectation(kind="clarify", allowed_tools=["query_hot_news"], resolution_codes=["ambiguous_requirements"])
    trace = {"name": "query_hot_news", "status": "failed", "attempts": 1,
             "result": {"query_resolution": {"status": "clarify", "reason_code": "ambiguous_requirements"}}}
    turn = {"status": "completed", "assistant_content": "请明确小时，尚未执行取数。", "tools": [trace]}
    assert evaluate_turn(expected, turn, dataset=dataset)["verdict"] == "passed"
    trace["result"]["workflow_id"] = "must-not-exist"
    assert "rejected_query_created_binding" in evaluate_turn(expected, turn, dataset=dataset)["failures"]


def test_followup_must_reuse_the_prior_run():
    dataset, _ = load_dataset()
    _, prior, details = successful_query()
    run_id = prior["tools"][0]["result"]["run_id"]
    turn = {"status": "completed", "assistant_content": run_id,
            "tools": [{"name": "read_hot_news", "status": "completed", "result": {"run_id": run_id, "items": report_items(details[run_id])}}]}
    expected = Expectation(kind="read", allowed_tools=["read_hot_news"], same_run_from_turn=0)
    assert evaluate_turn(expected, turn, dataset=dataset, previous_turns=[prior], run_details=details)["verdict"] == "passed"
    turn["tools"][0]["result"]["run_id"] = str(uuid4())
    assert "followup_run_reference_changed" in evaluate_turn(expected, turn, dataset=dataset, previous_turns=[prior])["failures"]


def test_pending_target_is_separate_from_passes_and_query_successes():
    dataset, _ = load_dataset()
    expected = Expectation(kind="pending", response_any=["明确"])
    turn = {"status": "completed", "assistant_content": "请明确完整查询。", "tools": []}
    grade = evaluate_turn(expected, turn, dataset=dataset)
    assert grade["verdict"] == "expected_pending"
    summary = summarize([{"verdict": grade["verdict"], "turns": [{"response": turn}]}])
    assert summary["passed"] == summary["completed_query_count"] == 0
    assert summary["expected_pending"] == 1


@pytest.mark.asyncio
async def test_runtime_must_be_local_before_any_conversation_or_message_is_created():
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200, json={"model_provider": "openai_compatible", "query_enabled": True})

    dataset, _ = load_dataset()
    with pytest.raises(ValueError, match="no messages"):
        await run_evaluation(dataset, selected_ids=["instruction_injection"], transport=httpx.MockTransport(reply))
    assert [request.method for request in calls] == ["GET"]


@pytest.mark.asyncio
async def test_http_timeout_records_blocked_without_retrying_message_submission():
    calls = []

    def reply(request):
        calls.append(request)
        if request.url.path.endswith("/runtime"):
            return httpx.Response(200, json={"model_provider": "local", "query_enabled": True})
        if request.url.path.endswith("/conversations"):
            return httpx.Response(201, json={"id": str(uuid4())})
        raise httpx.ReadTimeout("details must not leak", request=request)

    dataset, _ = load_dataset()
    report = await run_evaluation(dataset, selected_ids=["instruction_injection"], transport=httpx.MockTransport(reply))
    assert len([request for request in calls if request.url.path.endswith("/messages")]) == 1
    assert report["summary"]["blocked"] == 1
    assert report["summary"]["attempted_turn_count"] == report["summary"]["turn_count"] == 1
    assert report["summary"]["completed_response_count"] == 0
    assert report["cases"][0]["error"] == {"type": "ReadTimeout", "http_status": None, "submissions_retried": False}
    attempt = report["cases"][0]["turns"][0]
    assert attempt["user"] == dataset.cases[28].turns[0].user
    assert attempt["expected"]["kind"] == "refuse"
    assert attempt["request_id"] == json.loads(calls[-1].content)["request_id"]
    assert attempt["response"] is None and attempt["verdict"] == "blocked"
    assert attempt["elapsed_seconds"] >= 0
    assert attempt["error"] == report["cases"][0]["error"]
    assert "details must not leak" not in json.dumps(report)


@pytest.mark.asyncio
async def test_http_503_keeps_submitted_turn_and_safe_error_without_response_body():
    submissions = []

    def reply(request):
        if request.url.path.endswith("/runtime"):
            return httpx.Response(200, json={"model_provider": "local", "query_enabled": True})
        if request.url.path.endswith("/conversations"):
            return httpx.Response(201, json={"id": str(uuid4())})
        submissions.append(json.loads(request.content))
        return httpx.Response(503, json={"detail": "private database table and connection details"},
                              headers={"x-private-header": "private value"})

    dataset, _ = load_dataset()
    report = await run_evaluation(dataset, selected_ids=["sample_ctr_video"], transport=httpx.MockTransport(reply))
    assert len(submissions) == 1  # The failed first message prevents its dependent followup.
    assert report["summary"]["attempted_turn_count"] == 1
    assert report["summary"]["completed_response_count"] == report["summary"]["completed_query_count"] == 0
    attempt = report["cases"][0]["turns"][0]
    assert attempt["user"] == submissions[0]["content"]
    assert attempt["request_id"] == submissions[0]["request_id"]
    assert attempt["response"] is None and attempt["verdict"] == "blocked"
    assert attempt["error"] == {"type": "HTTPStatusError", "http_status": 503, "submissions_retried": False}
    assert "private" not in json.dumps(report)


@pytest.mark.asyncio
async def test_dialogue_transport_generates_unique_uuid_requests_and_keeps_responses():
    calls = []

    def reply(request):
        calls.append(request)
        if request.url.path.endswith("/runtime"):
            return httpx.Response(200, json={"model_provider": "local", "query_enabled": True})
        if request.url.path.endswith("/conversations"):
            return httpx.Response(201, json={"id": str(uuid4())})
        body = json.loads(request.content)
        return httpx.Response(200, json={"status": "completed", "user_content": body["content"],
                                        "request_id": body["request_id"], "assistant_content": "工具只读，不能删除数据。", "tools": []})

    dataset, _ = load_dataset()
    report = await run_evaluation(dataset, selected_ids=["instruction_injection"], direct_api_identity=True,
                                  transport=httpx.MockTransport(reply))
    assert report["summary"]["passed"] == 1
    assert report["summary"]["attempted_turn_count"] == report["summary"]["completed_response_count"] == 1
    assert report["cases"][0]["turns"][0]["response"]["user_content"] == dataset.cases[28].turns[0].user
    assert len({turn["request_id"] for case in report["cases"] for turn in case["turns"]}) == 1
    assert all(request.headers["x-hot-news-roles"] == "hot-news:admin" for request in calls)
    assert all(request.headers["x-user-id"] == LOCAL_USER for request in calls)
    assert "authorization" not in json.dumps(report).lower()

