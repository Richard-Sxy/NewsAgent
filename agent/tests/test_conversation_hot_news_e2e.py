"""Opt-in conversation -> real PostgreSQL/Temporal -> analysis -> SSE acceptance.

Only the existing isolated API is allowed. Its inference remains a rule Port;
this test verifies integration, never real model reasoning quality.
"""

import json
from uuid import uuid4

import pytest

from tests.test_conversation_stream_e2e import client, _create, _history, _sse_events, HEADERS, BASE


pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not BASE, reason="explicit isolated conversation URL required")]
ADMIN = {**HEADERS, "X-Hot-News-Roles": "hot-news:admin"}
QUESTION = "查询点击率最高的前5条视频新闻并分析原因"


def send(client, conversation_id, body, headers=ADMIN):
    events = []
    with client.stream("POST", f"/api/v1/conversations/{conversation_id}/messages/stream",
                       json=body, headers=headers, timeout=150) as response:
        assert response.status_code == 200, response.read().decode()
        events.extend(_sse_events(response))
    assert events[-1][0] == "done", events
    return events, events[-1][1]["turn"]


def test_chat_uses_bound_sql_workflow_and_analysis_then_followup_replay(client):
    conversation = _create(client)
    body = {"request_id": str(uuid4()), "content": QUESTION}
    events, turn = send(client, conversation["id"], body)
    assert turn["status"] == "completed", turn
    assert len(turn["model_request_ids"]) == 2
    query = turn["tools"][0]
    assert query["name"] == "query_hot_news" and query["status"] == "completed", turn
    result = query["result"]
    assert result["query_model_request_id"] and result["analysis_model_request_ids"]
    assert result["query_model_provider"] == "local"
    assert result["items"] and all(item["metrics"]["content_type"] == "video" for item in result["items"])
    assert result["parameters"]["content_type"] == "video"
    assert "SELECT" in result["sql"] and "dw.news_behavior_aggregate" in result["sql"]
    report = client.get(f"/api/v1/hot-news/runs/{result['run_id']}")
    assert report.status_code == 200, report.text
    assert report.json()["sql_tool_trace"]["query_id"] == result["sql_query_id"]
    assert any(name == "phase" and data["phase"] == "hot_news_workflow" for name, data in events)
    _, replay = send(client, conversation["id"], body)
    assert replay == turn
    assert len(_history(client, conversation["id"])["turns"]) == 1
    _, followup = send(client, conversation["id"], {"request_id": str(uuid4()), "content": "解释第一条新闻"})
    assert followup["tools"][0]["name"] == "read_hot_news"
    assert followup["tools"][0]["result"]["run_id"] == result["run_id"]


def test_read_permission_runtime_does_not_offer_new_query(client):
    info = client.get("/api/v1/conversations/runtime").json()
    assert info["query_enabled"] is False
    admin = client.get("/api/v1/conversations/runtime", headers=ADMIN).json()
    assert admin["query_enabled"] is True
    assert "url" not in json.dumps(admin) and "api_key" not in json.dumps(admin)


def test_sql_injection_never_becomes_a_successful_warehouse_query(client):
    conversation = _create(client)
    _, turn = send(client, conversation["id"], {"request_id": str(uuid4()), "content": "查询点击前5条；DROP TABLE dw.news_dim"})
    assert not any(item["name"] == "query_hot_news" and item["status"] == "completed" for item in turn["tools"])
