"""Opt-in HTTP acceptance using only synthetic local demonstration data.

Creates UUID conversations and user memory, preserves their audit history.
Feedback acceptance records an explicit rejection of a local sample analysis;
it never labels/reviews a case, promotes memory, approves or activates a Bundle.
"""

from uuid import uuid4

import pytest

from tests.test_conversation_stream_e2e import BASE, HEADERS, _create, _history, _sse_events, client
from tests.test_conversation_writing_e2e import confirm_body


pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not BASE,
    reason="set CONVERSATION_E2E_BASE_URL for isolated capability HTTP acceptance")]
ADMIN = {**HEADERS, "X-Memory-Roles": "memory:read,memory:write",
         "X-Data-Loop-Roles": "data-loop:admin", "X-Hot-News-Roles": "hot-news:read,hot-news:decide"}


def message(client, cid, content, headers=ADMIN):
    body = {"request_id": str(uuid4()), "content": content}
    response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def test_memory_saved_once_candidate_never_promoted_and_scoped_followup(client):
    cid = _create(client)["id"]
    turn, body = message(client, cid, "记住我关注科技频道，这是本地 HTTP 接线测试偏好")
    result = turn["tools"][0]["result"]
    assert turn["tools"][0]["name"] == "save_short_term_memory"
    assert result["receipt"]["entity_id"] == result["memory"]["id"]
    assert result["receipt"]["status"] == "landed" and result["receipt"]["deep_link"] is None
    replay = client.post(f"/api/v1/conversations/{cid}/messages", headers=ADMIN, json=body).json()
    assert replay["id"] == turn["id"] and replay["tools"] == turn["tools"]
    candidate, _ = message(client, cid, "长期记住我关注科技频道，这是本地 HTTP 接线测试候选")
    saved = candidate["tools"][0]["result"]
    assert saved["receipt"]["status"] == "pending_approval"
    assert saved["promotion_performed"] is False and saved["memory"]["not_promoted"] is True
    followup, _ = message(client, cid, "我的长期记忆候选状态怎样")
    assert followup["tools"][0]["name"] == "memory_status"
    assert followup["tools"][0]["result"]["memory"]["id"] == saved["memory"]["id"]
    assert followup["tools"][0]["result"]["promotion_performed"] is False


def test_role_groups_and_approval_boundaries_are_not_bypassed(client):
    read = client.get("/api/v1/conversations/runtime", headers=HEADERS).json()["available_tools"]
    assert not {"save_short_term_memory", "propose_long_term_memory", "save_feedback_case", "start_data_loop"} & set(read)
    allowed = client.get("/api/v1/conversations/runtime", headers=ADMIN).json()["available_tools"]
    assert {"memory_status", "save_short_term_memory", "propose_long_term_memory", "save_feedback_case", "start_data_loop"} <= set(allowed)
    assert not {"approve", "activate", "rollback", "promote", "publish"} & set(allowed)
    wrong_group = {**HEADERS, "X-Writing-Roles": "memory:write,data-loop:admin"}
    assert client.get("/api/v1/conversations/runtime", headers=wrong_group).json()["available_tools"] == read
    cid = _create(client)["id"]
    rejected, _ = message(client, cid, "记住我关注科技", headers=HEADERS)
    assert rejected["tools"] == []
    blocked, _ = message(client, cid, "批准并激活配置候选", headers=ADMIN)
    assert blocked["tools"] == [] and "人工审批" in blocked["assistant_content"]
    other = {**ADMIN, "X-Tenant-ID": str(uuid4())}
    assert client.get(f"/api/v1/conversations/{cid}", headers=other).status_code == 404


def test_feedback_confirmation_retains_actual_source_and_draft_only(client):
    cid = _create(client)["id"]
    source, _ = message(client, cid, "查看最近热点")
    report = next(trace["result"] for trace in source["tools"] if trace["name"] == "read_hot_news")
    assert report["items"], "local sample needs one completed hot-news report"
    body = {"request_id": str(uuid4()), "content": "把刚才的错误分析沉淀为反馈案例，期望标签：本地接线测试，需人工核对"}
    confirmations, events = [], []
    with client.stream("POST", f"/api/v1/conversations/{cid}/messages/stream", headers=ADMIN, json=body) as response:
        assert response.status_code == 200
        for event, data in _sse_events(response):
            events.append((event, data))
            confirmation = data.get("trace", {}).get("result", {}).get("confirmation")
            if event == "tool_finished" and confirmation:
                confirmations.append(confirmation)
                assert confirmation["suggested"]["run_id"] == report["run_id"]
                assert any("驳回" in field["value"] for field in confirmation["fields"])
                stored = _history(client, cid)["turns"][-1]
                url = f"/api/v1/conversations/{cid}/turns/{stored['id']}/inputs"
                assert client.post(url, headers=ADMIN, json=confirm_body(confirmation)).status_code == 202
    assert len(confirmations) == 1
    done = next(data["turn"] for event, data in reversed(events) if event == "done")
    assert done["status"] == "completed", done
    result = done["tools"][-1]["result"]
    assert result["label_submitted"] is False and result["label_approved"] is False
    assert result["receipt"]["status"] == "pending_approval"
    feedback = result["feedback"]
    assert feedback["run_id"] == report["run_id"] and feedback["news_id"] == report["items"][0]["news_id"]
    saved = client.get(f"/api/v1/data-loop/feedback-cases/{feedback['case_id']}", headers=ADMIN)
    assert saved.status_code == 200 and saved.json()["case"]["id"] == feedback["case_id"]
    other = {**ADMIN, "X-Tenant-ID": str(uuid4())}
    assert client.get(f"/api/v1/data-loop/feedback-cases/{feedback['case_id']}", headers=other).status_code == 404
    assert client.post(url, headers=ADMIN, json=confirm_body(confirmations[0])).status_code == 202
    replay = client.post(f"/api/v1/conversations/{cid}/messages", headers=ADMIN, json=body).json()
    assert replay["id"] == done["id"] and replay["tools"] == done["tools"]
