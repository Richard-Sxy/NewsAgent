"""Opt-in writing confirmation against the existing isolated API/worker stack.

CONVERSATION_E2E_BASE_URL is the same local allowlist as the conversation SSE
suite. Tests preserve new synthetic conversations/jobs and never approve or
publish an article, change a bundle, reset a database or read .env.
"""

import json
import time
from uuid import uuid4

import pytest

from tests.test_conversation_stream_e2e import (BASE, HEADERS, _create, _history,
                                               _sse_events, client)


pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not BASE,
    reason="set CONVERSATION_E2E_BASE_URL for isolated writing confirmation acceptance")]
ADMIN = {**HEADERS, "X-Writing-Roles": "writing:admin"}
READ = {**HEADERS, "X-Writing-Roles": "writing:read"}


def confirm_body(confirmation, action="confirm"):
    return {"request_id": confirmation["input_request_id"],
            "content": "capability-confirm:" + json.dumps(
                {"token": confirmation["token"], "action": action}, separators=(",", ":"))}


def test_writing_permission_runtime_matrix_and_rejection(client):
    readonly = client.get("/api/v1/conversations/runtime", headers=READ).json()["available_tools"]
    admin = client.get("/api/v1/conversations/runtime", headers=ADMIN).json()["available_tools"]
    assert {"list_writing_jobs", "writing_job_status"} <= set(readonly)
    assert "start_writing_intent" not in readonly and "start_writing_intent" in admin
    conversation = _create(client)
    response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", headers=READ,
        json={"request_id": str(uuid4()), "content": "写一篇 AI 芯片周报"})
    assert response.status_code == 200 and response.json()["tools"] == []


def test_confirmed_start_http_input_replay_history_and_live_job(client):
    conversation = _create(client)
    cid = conversation["id"]
    body = {"request_id": str(uuid4()), "content": "写一篇 M2 HTTP 验收 AI 芯片周报，1500字"}
    confirmation = None
    with client.stream("POST", f"/api/v1/conversations/{cid}/messages/stream", headers=ADMIN, json=body) as response:
        assert response.status_code == 200
        events = []
        for event, data in _sse_events(response):
            events.append((event, data))
            if event == "tool_finished" and data.get("trace", {}).get("result", {}).get("confirmation"):
                confirmation = data["trace"]["result"]["confirmation"]
                stored = _history(client, cid)["turns"][-1]
                assert stored["tools"][0] == data["trace"]
                assert stored["tools"][0]["result"]["receipt"]["status"] == "not_landed"
                url = f"/api/v1/conversations/{cid}/turns/{stored['id']}/inputs"
                assert client.post(url, json=confirm_body(confirmation), headers=ADMIN).status_code == 202
    assert confirmation is not None
    done = [data["turn"] for event, data in events if event == "done"][-1]
    assert done["status"] == "completed" and len(done["tools"]) == 2
    result = done["tools"][-1]["result"]
    assert result["workflow_accepted"] is True and result["receipt"]["entity_id"] == result["job_id"]
    assert done["inputs"][0]["status"] == "applied"
    count = len(_history(client, cid)["turns"])
    assert client.post(url, json=confirm_body(confirmation), headers=ADMIN).status_code == 202
    replay = client.post(f"/api/v1/conversations/{cid}/messages", headers=ADMIN, json=body).json()
    assert replay["id"] == done["id"] and replay["tools"] == done["tools"]
    assert len(_history(client, cid)["turns"]) == count
    job = client.get(f"/api/v1/jobs/{result['job_id']}").json()
    assert job["temporal_workflow_id"] == result["workflow_id"]
    assert job["requirements"]["target_words"] == 1500
    followup = client.post(f"/api/v1/conversations/{cid}/messages", headers=READ,
        json={"request_id": str(uuid4()), "content": "那个任务进度怎样"}).json()
    assert followup["tools"][0]["name"] == "writing_job_status"
    assert followup["tools"][0]["result"]["receipt"]["entity_id"] == job["id"]


def test_cancelled_confirmation_does_not_start_job(client):
    cid = _create(client)["id"]
    with client.stream("POST", f"/api/v1/conversations/{cid}/messages/stream", headers=ADMIN,
        json={"request_id": str(uuid4()), "content": "写一篇 M2 取消验收周报"}) as response:
        events = []
        for event, data in _sse_events(response):
            events.append((event, data))
            pending = data.get("trace", {}).get("result", {}).get("confirmation")
            if event == "tool_finished" and pending:
                turn = _history(client, cid)["turns"][-1]
                outcome = client.post(f"/api/v1/conversations/{cid}/turns/{turn['id']}/inputs",
                    headers=ADMIN, json=confirm_body(pending, "cancel"))
                assert outcome.status_code == 202
    done = [data["turn"] for event, data in events if event == "done"][-1]
    assert done["tools"][-1]["result"] == {"confirmation_outcome": "cancelled"}
    assert all("job_id" not in trace["result"] for trace in done["tools"])


def test_disconnect_before_confirmation_preserves_proposal_without_job(client):
    cid = _create(client)["id"]
    with client.stream("POST", f"/api/v1/conversations/{cid}/messages/stream", headers=ADMIN,
        json={"request_id": str(uuid4()), "content": "写一篇 M2 断线未确认验收稿"}) as response:
        for event, data in _sse_events(response):
            if event == "tool_finished" and data.get("trace", {}).get("result", {}).get("confirmation"):
                break
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        turn = _history(client, cid)["turns"][-1]
        if turn["status"] != "processing":
            break
        time.sleep(0.2)
    assert turn["status"] == "failed" and turn["error_code"] == "interrupted"
    assert turn["tools"][0]["result"]["confirmation"]
    assert all("job_id" not in trace["result"] for trace in turn["tools"])
