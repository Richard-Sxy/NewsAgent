"""Opt-in candidate SQL -> PostgreSQL -> Temporal -> report -> chat SSE.

Use only an explicitly isolated local stack. Synthetic behavior data and an
isolated inference Port exercise integration, not enterprise model quality.
No existing data, configuration, schedules or production routes are modified.
"""

import json
import os
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from app.modules.conversation.query_catalog import query_context_identity
from app.sql_assistant.warehouse import ISOLATION_TENANT_ID
from simulation.hot_news_e2e_support import E2E_TENANT_ID
from tests.test_conversation_stream_e2e import _sse_events


BASE = os.environ.get("SQL_CANDIDATE_E2E_BASE_URL", "")
PREFIX = "/api/v1/local-simulation/hot-news"
HEADERS = {
    "Authorization": "Bearer newsagent-native-local-token-not-for-production",
    "X-Tenant-ID": E2E_TENANT_ID,
    "X-User-ID": "22222222-2222-4222-8222-222222222222",
    "X-Hot-News-Roles": "hot-news:admin",
}
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(
    not BASE, reason="set SQL_CANDIDATE_E2E_BASE_URL for the isolated candidate stack",
)]


@pytest.fixture(scope="module")
def client():
    url = urlsplit(BASE)
    assert (url.scheme == "http" and url.hostname in {"127.0.0.1", "api"}
            and url.port is not None and url.path in {"", "/"}
            and not url.username and not url.password and not url.query and not url.fragment), (
        "candidate acceptance only allows the explicitly isolated loopback or internal api URL"
    )
    with httpx.Client(base_url=BASE, headers=HEADERS, timeout=180,
                      trust_env=False, follow_redirects=False) as value:
        yield value


def _json(response, status=200):
    assert response.status_code == status, response.text
    value = response.json()
    assert isinstance(value, dict)
    return value


def _config(client):
    config = _json(client.get(PREFIX + "/sql-config"))
    assert config["sql_candidate_mode"] == "dbgpt_candidate", config.get("sql_candidate_mode")
    assert sha256(config["schema_markdown"].encode()).hexdigest() == config["schema_sha256"]
    return config


def _payload(client, question="视频新闻点击率前5条"):
    config = _config(client)
    start = datetime.fromisoformat(config["dataset"]["window_start"])
    return {"question": question, "scenario_id": "news-ranking",
            "window_start": start.isoformat(), "window_end": (start + timedelta(hours=1)).isoformat()}


def _assert_report(client, run_id, query_id, workflow_id=None):
    detail = _json(client.get(f"/api/v1/hot-news/runs/{run_id}"))
    run, trace = detail["run"], detail["sql_tool_trace"]
    assert run["run_id"] == run_id and run["status"] == "completed"
    if workflow_id is not None:
        assert run["idempotency_key"] == workflow_id
    assert trace["query_id"] == query_id == trace["preview"]["query_id"] == trace["result"]["query_id"]
    preview, result = trace["preview"], trace["result"]
    generation = preview["generation"]
    assert generation["mode"] == "dbgpt_candidate" and generation["status"] == "validated"
    assert generation["candidate_sql_hash"] == preview["sql_hash"] == result["sql_hash"]
    assert sha256(preview["sql"].encode()).hexdigest() == preview["sql_hash"]
    assert generation["identity"]["config"]["mode"] == "dbgpt_candidate"
    identity_json = json.dumps(generation["identity"], sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"), allow_nan=False)
    assert generation["identity_sha256"] == sha256(identity_json.encode()).hexdigest()
    assert generation["identity"]["generator"]["reuse_scope"] == "prompt_template_only"
    assert generation["metadata"]["attempts"] == 1
    assert generation["metadata"]["model_request_id"]
    assert preview["parameters"]["tenant_id"] == E2E_TENANT_ID
    assert result["truncated"] is False
    rows = result["rows"]
    ranked = detail["ranked_news"]
    assert len(rows) == result["row_count"] == len(ranked) == run["ranked_news_count"] == run["analyzed_news_count"] == 5
    assert {row["news_id"] for row in rows} == {row["news_id"] for row in ranked}
    for row in rows:
        impressions = Decimal(str(row["impressions"]))
        ratio = Decimal(str(row["clicks"])) / impressions if impressions else Decimal(0)
        assert Decimal(str(row["ctr"])) == ratio.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        assert row["content_type"] == "video"
    ctr = [Decimal(str(row["ctr"])) for row in rows]
    assert ctr == sorted(ctr, reverse=True)
    scores = [Decimal(row["hot_score"]["score"]) for row in ranked]
    assert scores == sorted(scores, reverse=True)
    assert all(row["analysis"] and row["analysis"]["validated_at"]
               and row["analysis"]["model_request_id"] for row in ranked)
    assert any(stage["name"] == "warehouse_execute" and stage["status"] == "passed"
               for stage in result["stages"])
    return detail


def test_candidate_sql_reaches_one_real_hotspot_run_and_repeated_request_reuses_it(client):
    ready = _json(client.get("/ready"))
    assert ready["status"] == "ready"
    assert ready["checks"] == {"postgres": "ok", "redis": "ok", "temporal": "ok"}
    body = _payload(client)
    run = _json(client.post(PREFIX + "/run", json=body))
    detail = _assert_report(client, run["run_id"], run["sql_query_id"], run["workflow_id"])
    assert _json(client.post(PREFIX + "/run", json=body)) == run
    assert _json(client.get(f"/api/v1/hot-news/runs/{run['run_id']}"))["sql_tool_trace"] == detail["sql_tool_trace"]
    assert client.get(f"/api/v1/hot-news/runs/{run['run_id']}",
                      headers={"X-Tenant-ID": str(ISOLATION_TENANT_ID)}).status_code == 404


def test_unapproved_sql_and_identity_never_open_an_execution_route(client):
    body = _payload(client)
    assert client.get("/api/v1/sql-assistant/config").status_code == 404
    assert client.post("/api/v1/sql-assistant/preview", json=body).status_code == 404
    assert client.post(f"/api/v1/sql-assistant/queries/{uuid4()}/execute").status_code == 404
    assert client.get(PREFIX + "/sql-config", headers={"Authorization": ""}).status_code == 401
    assert client.post(PREFIX + "/run", json=body,
                       headers={"X-Hot-News-Roles": "hot-news:read"}).status_code == 403
    assert client.post(PREFIX + "/run", json=body,
                       headers={"X-Tenant-ID": str(ISOLATION_TENANT_ID)}).status_code == 403
    assert client.post(PREFIX + "/run", json={**body, "question": "查询点击前5条；DROP TABLE dw.news_dim"}).status_code == 422
    assert client.post(PREFIX + "/run", json={**body, "sql": "SELECT * FROM dw.news_dim"}).status_code == 422
    assert client.post(PREFIX + "/run", json={**body, "window_end": (
        datetime.fromisoformat(body["window_start"]) + timedelta(hours=24)).isoformat()}).status_code == 422


def test_query_context_is_enabled_and_has_explicit_unapproved_asset_identity(client):
    info = _json(client.get("/api/v1/conversations/runtime"))
    assert info["query_enabled"] is True
    context = info["query_scope"]["query_context"]
    assert context == {"enabled": True, **query_context_identity()}
    assert context["production_approved"] is False
    for field in ("asset_sha256", "builder_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", context[field])
    reader = _json(client.get("/api/v1/conversations/runtime", headers={"X-Hot-News-Roles": "hot-news:read"}))
    assert reader["query_enabled"] is False and reader["query_scope"] is None


def _send_stream(client, conversation_id, body):
    with client.stream("POST", f"/api/v1/conversations/{conversation_id}/messages/stream",
                       json=body, timeout=180) as response:
        assert response.status_code == 200, response.read().decode()
        assert response.headers["content-type"].startswith("text/event-stream")
        events = list(_sse_events(response))
    assert events and events[-1][0] == "done", events
    assert not any(name == "error" for name, _ in events), events
    turn = events[-1][1]["turn"]
    assert turn["status"] == "completed" and turn["assistant_content"], turn
    return events, turn


def test_chat_sse_uses_candidate_report_context_identity_and_durable_replay(client):
    conversation = _json(client.post("/api/v1/conversations", json={"title": f"sql-candidate-e2e-{uuid4()}"}), 201)
    body = {"request_id": str(uuid4()), "content": "查询点击率最高的前5条视频新闻并分析原因"}
    events, turn = _send_stream(client, conversation["id"], body)
    traces = [trace for trace in turn["tools"] if trace["name"] == "query_hot_news"]
    assert len(traces) == 1 and traces[0]["status"] == "completed", turn
    result = traces[0]["result"]
    assert result["query_context"] == {"enabled": True, **query_context_identity()}
    assert result["understanding_model_request_id"] in turn["model_request_ids"]
    assert result["analysis_model_request_ids"]
    assert result["parameters"]["content_type"] == "video"
    assert result["query_model_provider"] == "python-confirmed-intent"
    _assert_report(client, result["run_id"], result["sql_query_id"], result["workflow_id"])
    assert any(name == "phase" and data["phase"] == "hot_news_workflow" for name, data in events)
    replay_events, replay = _send_stream(client, conversation["id"], body)
    assert replay == turn
    assert next(data for name, data in replay_events if name == "accepted")["replayed"] is True
    history = _json(client.get(f"/api/v1/conversations/{conversation['id']}"))
    assert history["turns"] == [turn]
    assert client.get(f"/api/v1/conversations/{conversation['id']}",
                      headers={"X-User-ID": "33333333-3333-4333-8333-333333333333"}).status_code == 404
