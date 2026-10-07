"""Opt-in SSE acceptance for the isolated conversation API and real history.

Set CONVERSATION_E2E_BASE_URL explicitly to http://127.0.0.1:28000 or
http://api:8000 after deploying the stream route. The hotspot test requires an
existing completed native-demo run; it reads that run and never starts one.

Progress events describe the bounded Agent execution. Answer deltas deliver a
validated, persisted answer in pieces; they do not represent raw model tokens.
Every test creates a UUID-named conversation and preserves its history. No
database reset, deletion, configuration change, publishing or .env read occurs.
"""

from __future__ import annotations

import codecs
import json
import os
from uuid import UUID, uuid4

import httpx
import pytest


BASE = os.environ.get("CONVERSATION_E2E_BASE_URL", "")
PREFIX = "/api/v1/conversations"
HEADERS = {
    "Authorization": "Bearer newsagent-native-local-token-not-for-production",
    "X-Tenant-ID": "11111111-1111-4111-8111-111111111111",
    "X-User-ID": "22222222-2222-4222-8222-222222222222",
    "X-Hot-News-Roles": "hot-news:read",
}
READ_ONLY_TOOLS = {"capabilities", "list_hot_news", "read_hot_news", "search_knowledge"}
EVENT_NAMES = {"accepted", "phase", "tool_started", "tool_finished", "answer_delta", "done", "error"}
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not BASE,
        reason="set CONVERSATION_E2E_BASE_URL for isolated conversation SSE acceptance",
    ),
]


@pytest.fixture(scope="module")
def client():
    assert BASE in {"http://127.0.0.1:28000", "http://api:8000"}, (
        "conversation SSE acceptance only allows the explicit local demo API"
    )
    with httpx.Client(
        base_url=BASE,
        headers=HEADERS,
        timeout=120,
        trust_env=False,
        follow_redirects=False,
    ) as value:
        yield value


def _json(response: httpx.Response, expected_status: int = 200) -> dict:
    assert response.status_code == expected_status, response.text
    value = response.json()
    assert isinstance(value, dict)
    return value


def _create(client: httpx.Client) -> dict:
    title = f"conversation-stream-e2e-{uuid4()}"
    value = _json(client.post(PREFIX, json={"title": title}), 201)
    UUID(value["id"])
    assert value["title"] == title
    return value


def _history(client: httpx.Client, conversation_id: str) -> dict:
    value = _json(client.get(f"{PREFIX}/{conversation_id}"))
    assert value["conversation"]["id"] == conversation_id
    assert isinstance(value["turns"], list)
    return value


def _sse_events(response: httpx.Response):
    """Strictly decode split UTF-8 bytes and SSE frames, including CRLF/comments."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    buffer = ""
    event_name = "message"
    data_lines = []
    for chunk in response.iter_bytes():
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.removesuffix("\r")
            if not line:
                if data_lines:
                    payload = json.loads("\n".join(data_lines))
                    assert isinstance(payload, dict), "SSE data must be a JSON object"
                    yield event_name, payload
                event_name, data_lines = "message", []
                continue
            if line.startswith(":"):
                # Heartbeats are transport comments; no execution or answer data.
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value)
    buffer += decoder.decode(b"", final=True)
    assert not buffer.strip() and not data_lines, "SSE must end with a complete event frame"


def _assert_turn(turn: dict, request_id: str) -> None:
    UUID(turn["id"])
    assert turn["request_id"] == request_id
    assert turn["status"] == "completed", turn.get("error_code")
    assert turn["assistant_content"] and turn["completed_at"]
    assert turn["error_code"] is None
    assert turn["model_request_ids"]
    assert all(isinstance(item, str) and item for item in turn["model_request_ids"])
    for trace in turn["tools"]:
        assert trace["name"] in READ_ONLY_TOOLS
        assert trace["status"] in {"completed", "failed", "denied"}
        assert 0 <= trace["attempts"] <= 2
        assert isinstance(trace["arguments"], dict) and isinstance(trace["result"], dict)
        assert not ({"tenant_id", "user_id", "permissions"} & trace["arguments"].keys())


def _stream(
    client: httpx.Client,
    conversation_id: str,
    content: str,
    request_id: str | None = None,
    *,
    replayed: bool = False,
) -> dict:
    request_id = request_id or str(uuid4())
    events = []
    persisted_at_first_delta = None
    with client.stream(
        "POST",
        f"{PREFIX}/{conversation_id}/messages/stream",
        json={"request_id": request_id, "content": content},
        timeout=120,
    ) as response:
        assert response.status_code == 200, response.read().decode("utf-8")
        content_type = response.headers.get("content-type", "").lower()
        assert content_type.startswith("text/event-stream")
        assert "charset=utf-8" in content_type
        cache_control = response.headers.get("cache-control", "").lower()
        assert "no-cache" in cache_control or "no-store" in cache_control
        for name, data in _sse_events(response):
            assert name in EVENT_NAMES, f"unknown stream event: {name}"
            assert name != "error", data
            events.append((name, data))
            if name == "answer_delta" and persisted_at_first_delta is None:
                # The HTTP stream is still open here. The answer must already be
                # committed before Python starts emitting its display pieces.
                history = _history(client, conversation_id)
                matches = [turn for turn in history["turns"] if turn["request_id"] == request_id]
                assert len(matches) == 1
                persisted_at_first_delta = matches[0]
                assert persisted_at_first_delta["status"] in {"completed", "failed"}

    assert events and events[0][0] == "accepted"
    accepted = [data for name, data in events if name == "accepted"]
    assert len(accepted) == 1
    assert accepted[0]["request_id"] == request_id
    assert accepted[0]["replayed"] is replayed
    assert accepted[0]["status"] in {"processing", "completed", "failed"}
    UUID(accepted[0]["turn_id"])
    done = [data for name, data in events if name == "done"]
    assert len(done) == 1 and events[-1][0] == "done"
    turn = done[0]["turn"]
    _assert_turn(turn, request_id)
    assert turn["id"] == accepted[0]["turn_id"]
    assert turn["user_content"] == content.strip()
    phases = [data for name, data in events if name == "phase"]
    for phase in phases:
        assert isinstance(phase["phase"], str) and phase["phase"]
        assert isinstance(phase["message"], str) and phase["message"]
    if not replayed:
        assert phases, "a new streamed turn must expose execution progress"
    deltas = [data for name, data in events if name == "answer_delta"]
    assert deltas
    indices = [item["index"] for item in deltas]
    assert all(isinstance(index, int) and index >= 0 for index in indices)
    assert indices == list(range(indices[0], indices[0] + len(indices)))
    for delta in deltas:
        assert delta["turn_id"] == turn["id"]
        assert delta["request_id"] == request_id
        assert isinstance(delta["text"], str) and delta["text"]
    assert "".join(delta["text"] for delta in deltas) == turn["assistant_content"]
    assert persisted_at_first_delta == turn
    history = _history(client, conversation_id)
    assert [item for item in history["turns"] if item["request_id"] == request_id] == [turn]
    return {"turn": turn, "events": events}


def _trace(turn: dict, name: str) -> dict:
    traces = [trace for trace in turn["tools"] if trace["name"] == name]
    assert len(traces) == 1
    assert traces[0]["status"] == "completed", traces[0]
    return traces[0]


def _assert_tool_progress(result: dict) -> None:
    traces = result["turn"]["tools"]
    starts = [data for name, data in result["events"] if name == "tool_started"]
    finishes = [data for name, data in result["events"] if name == "tool_finished"]
    assert len(starts) == len(finishes) == len(traces)
    for started, finished, trace in zip(starts, finishes, traces, strict=True):
        assert started["index"] == finished["index"]
        assert started["name"] == finished["name"] == trace["name"]
        assert finished["status"] == trace["status"]
        assert finished["attempts"] == trace["attempts"]
        assert finished["error_code"] == trace["error_code"]
        start_position = result["events"].index(("tool_started", started))
        finish_position = result["events"].index(("tool_finished", finished))
        assert start_position < finish_position


def _pre_stream_error(client: httpx.Client, path: str, body: dict, status: int, headers=None):
    with client.stream("POST", path, json=body, headers=headers, timeout=120) as response:
        # Validation/auth/scope/claim errors must be ordinary HTTP responses,
        # before a 200 SSE response is committed to the client.
        assert response.status_code == status, response.read().decode("utf-8")
        assert "text/event-stream" not in response.headers.get("content-type", "").lower()
        value = json.loads(response.read())
        assert isinstance(value, dict) and "detail" in value


def test_stream_headers_utf8_progress_answer_and_persisted_terminal_turn(client):
    conversation = _create(client)
    result = _stream(client, conversation["id"], "你好，中文交互")
    assert any(ord(character) > 127 for character in result["turn"]["assistant_content"])
    assert result["turn"]["tools"] == []
    assert _history(client, conversation["id"])["turns"] == [result["turn"]]


def test_same_request_stream_replay_adds_no_turn_or_model_request(client):
    conversation = _create(client)
    request_id = str(uuid4())
    first = _stream(client, conversation["id"], "你好", request_id)
    before = _history(client, conversation["id"])
    replay = _stream(client, conversation["id"], "  你好  ", request_id, replayed=True)
    assert replay["turn"] == first["turn"]
    assert not any(name in {"tool_started", "tool_finished"} for name, _data in replay["events"])
    assert _history(client, conversation["id"]) == before


def test_original_json_endpoint_and_stream_share_history_and_request_replay(client):
    conversation = _create(client)
    original_request = str(uuid4())
    original = _json(client.post(
        f"{PREFIX}/{conversation['id']}/messages",
        json={"request_id": original_request, "content": "你好"},
    ))
    _assert_turn(original, original_request)
    followup = _stream(client, conversation["id"], "记得上一轮我说了什么吗")
    assert "你好" in followup["turn"]["assistant_content"]
    replay_as_json = _json(client.post(
        f"{PREFIX}/{conversation['id']}/messages",
        json={"request_id": followup["turn"]["request_id"], "content": "记得上一轮我说了什么吗"},
    ))
    assert replay_as_json == followup["turn"]
    assert _history(client, conversation["id"])["turns"] == [original, followup["turn"]]


def test_stream_hot_report_and_followup_keep_same_run_and_authoritative_data(client):
    conversation = _create(client)
    first = _stream(client, conversation["id"], "查看最近热点")
    _assert_tool_progress(first)
    listed = _trace(first["turn"], "list_hot_news")
    assert listed["result"]["items"], "an existing completed local hot-news run is required"
    run_id = listed["result"]["items"][0]["run_id"]
    reading = _trace(first["turn"], "read_hot_news")
    assert reading["arguments"]["run_id"] == reading["result"]["run_id"] == run_id
    source = _json(client.get(f"/api/v1/hot-news/runs/{run_id}"))
    assert source["run"]["status"] == "completed" and source["ranked_news"]
    ranked = {item["rank"]: item for item in source["ranked_news"]}
    for item in reading["result"]["items"]:
        assert item["news_id"] == ranked[item["rank"]]["news_id"]
        assert item["metrics"] == ranked[item["rank"]]["metrics"]
        assert item["hot_score"] == ranked[item["rank"]]["hot_score"]
    followup = _stream(client, conversation["id"], "解释第一条新闻")
    _assert_tool_progress(followup)
    read_followup = _trace(followup["turn"], "read_hot_news")
    assert read_followup["arguments"] == {"run_id": run_id, "news_rank": 1}
    assert read_followup["result"]["run_id"] == run_id
    assert len(read_followup["result"]["items"]) == 1
    assert read_followup["result"]["items"][0]["news_id"] == ranked[1]["news_id"]
    assert _history(client, conversation["id"])["turns"] == [first["turn"], followup["turn"]]


def test_stream_auth_roles_and_cross_identity_fail_before_sse_headers(client):
    conversation = _create(client)
    path = f"{PREFIX}/{conversation['id']}/messages/stream"
    body = {"request_id": str(uuid4()), "content": "你好"}
    _pre_stream_error(client, path, body, 401, {"Authorization": ""})
    _pre_stream_error(client, path, body, 403, {"X-Hot-News-Roles": "hot-news:decide"})
    for headers in ({"X-User-ID": str(uuid4())}, {"X-Tenant-ID": str(uuid4())}):
        _pre_stream_error(client, path, body, 404, headers)
    assert _history(client, conversation["id"])["turns"] == []


def test_stream_invalid_uuid_content_and_extra_fields_fail_before_sse_headers(client):
    conversation = _create(client)
    path = f"{PREFIX}/{conversation['id']}/messages/stream"
    bodies = [
        {"request_id": "not-a-uuid", "content": "你好"},
        {"request_id": str(uuid4()), "content": " \n "},
        {"request_id": str(uuid4()), "content": "x" * 4001},
        {"request_id": str(uuid4()), "content": "你好", "tenant_id": str(uuid4())},
        {"request_id": str(uuid4()), "content": "你好", "user_id": str(uuid4())},
    ]
    for body in bodies:
        _pre_stream_error(client, path, body, 422)
    _pre_stream_error(
        client, f"{PREFIX}/not-a-uuid/messages/stream",
        {"request_id": str(uuid4()), "content": "你好"}, 422,
    )
    assert _history(client, conversation["id"])["turns"] == []


def test_stream_request_content_conflict_is_http_409_and_preserves_history(client):
    conversation = _create(client)
    request_id = str(uuid4())
    first = _stream(client, conversation["id"], "你好", request_id)
    before = _history(client, conversation["id"])
    _pre_stream_error(
        client, f"{PREFIX}/{conversation['id']}/messages/stream",
        {"request_id": request_id, "content": "内容不同的新请求"}, 409,
    )
    after = _history(client, conversation["id"])
    assert after == before and after["turns"] == [first["turn"]]
