"""Opt-in HTTP/SSE acceptance for synthetic local Data Loop conversations.

Run only after seed_conversation_evaluation_demo.py --apply and deploying the
local trusted analysis_prompt_version=conversation-demo-candidate-v1 preset.
Set CONVERSATION_DATALOOP_E2E_BASE_URL=http://api:8000 inside that isolated
Docker network, or use one of the explicit local addresses below. No .env is
read. This module creates exactly one candidate, conversation and evaluation;
it preserves them for inspection. It never approves, activates, labels, resets
or deletes anything. The two label actors in the pre-existing seed are test
actors: these fixtures do not demonstrate human review or enterprise quality.

An accepted admission request may be queued without a business Workflow. Only
read-only receipt polling proves business acceptance and eventual evaluation.
Existing jobs waiting for human approval may occupy the tenant budget; this
suite fails with that state instead of approving or cancelling them to free it.

For a GET-only recheck without creating another task, set
CONVERSATION_DATALOOP_E2E_EXISTING_CONVERSATION_ID and select only
test_existing_receipt_entities_are_tenant_scoped_readonly. It reads references
from that synthetic conversation's saved traces and never invokes the flow
fixture, a model, confirmation input or any write endpoint.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest

from tests.test_conversation_stream_e2e import _sse_events


BASE = os.environ.get("CONVERSATION_DATALOOP_E2E_BASE_URL", "")
EXISTING_CONVERSATION_ID = os.environ.get("CONVERSATION_DATALOOP_E2E_EXISTING_CONVERSATION_ID", "")
CONVERSATIONS = "/api/v1/conversations"
DATA_LOOP = "/api/v1/data-loop"
HEADERS = {
    "Authorization": "Bearer newsagent-native-local-token-not-for-production",
    "X-Tenant-ID": "11111111-1111-4111-8111-111111111111",
    "X-User-ID": "22222222-2222-4222-8222-222222222222",
    "X-Hot-News-Roles": "hot-news:read",
}
ADMIN = {**HEADERS, "X-Data-Loop-Roles": "data-loop:admin"}
READ = {**HEADERS, "X-Data-Loop-Roles": "data-loop:read"}
PRESET = "conversation-demo-candidate-v1"
LAYERS = {"golden", "fresh_bad_case", "high_risk_regression"}
WORKFLOW_ID = re.compile(r"^hot-news-data-loop-[0-9a-f]{64}$")
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BASE, reason="set CONVERSATION_DATALOOP_E2E_BASE_URL for isolated M4 acceptance"),
]


def _json(response: httpx.Response, expected=200) -> dict:
    assert response.status_code == expected, response.text
    value = response.json()
    assert isinstance(value, dict)
    return value


def _history(client, conversation_id):
    value = _json(client.get(f"{CONVERSATIONS}/{conversation_id}"))
    assert value["conversation"]["id"] == conversation_id
    return value


def _turn(client, conversation_id, request_id):
    turns = [item for item in _history(client, conversation_id)["turns"]
             if item["request_id"] == request_id]
    assert len(turns) == 1
    return turns[0]


def _confirmation_body(confirmation):
    return {"request_id": confirmation["input_request_id"],
            "content": "capability-confirm:" + json.dumps(
                {"token": confirmation["token"], "action": "confirm"}, separators=(",", ":"))}


def _assert_accepted(result):
    assert WORKFLOW_ID.fullmatch(result["workflow_id"])
    assert result["run"]["workflow_id"] == result["workflow_id"]
    # A queued admission is a saved request, not proof of business execution.
    if result["workflow_accepted"] is True:
        assert result["receipt"]["entity_id"] == result["workflow_id"]
        assert result["receipt"]["status"] in {"landed", "pending_approval"}
    else:
        assert result["workflow_accepted"] is False
        assert result["admission_accepted"] is True
        assert result["admission"]["state"] in {"queued", "granted", "starting", "running"}
        assert result["receipt"]["status"] == "not_landed"
        assert result["receipt"]["entity_id"] is None
        assert result["run"]["evaluation_run_id"] is None


def _confirmed_stream(client, conversation_id, body, hook, validate_proposal, *, disconnect=False):
    proposal, confirmed, accepted, done, input_row = None, None, None, None, None
    with client.stream("POST", f"{CONVERSATIONS}/{conversation_id}/messages/stream",
                       json=body) as response:
        assert response.status_code == 200, response.read().decode("utf-8")
        assert response.headers["content-type"].startswith("text/event-stream")
        for event, data in _sse_events(response):
            assert event != "error", data
            if event == "accepted":
                accepted = data
                assert data["request_id"] == body["request_id"] and data["replayed"] is False
                UUID(data["turn_id"])
            elif event == "tool_finished" and data.get("name") == hook:
                trace = data["trace"]
                assert trace["status"] == "completed", trace.get("error_code")
                persisted = _turn(client, conversation_id, body["request_id"])
                # Read while SSE is open: the trace is committed before the
                # proposal card or completed execution is announced to clients.
                assert persisted["id"] == accepted["turn_id"]
                assert persisted["tools"][data["index"]] == trace
                confirmation = trace["result"].get("confirmation")
                if confirmation:
                    assert proposal is None, "a confirmed operation must not ask the model again"
                    proposal = trace
                    assert confirmation["hook"] == hook and confirmation["state"] == "pending"
                    assert confirmation["provenance"] == "model_suggestion"
                    assert trace["result"]["receipt"]["status"] == "not_landed"
                    validate_proposal(trace)
                    url = f"{CONVERSATIONS}/{conversation_id}/turns/{persisted['id']}/inputs"
                    input_row = _json(client.post(url, json=_confirmation_body(confirmation)), 202)
                else:
                    assert proposal is not None and confirmed is None
                    confirmed = trace
                    assert trace["attempts"] == 1
                    if disconnect:
                        # Deliberately close before done; the business reference
                        # above was already read from durable conversation history.
                        break
            elif event == "done":
                done = data["turn"]
    assert accepted is not None and proposal is not None and confirmed is not None
    if not disconnect:
        assert done is not None and done["status"] == "completed", done
        assert done["tools"] == [proposal, confirmed]
    deadline = time.monotonic() + 15
    while True:
        stored = _turn(client, conversation_id, body["request_id"])
        if stored["status"] != "processing":
            break
        assert time.monotonic() < deadline, "stream disconnect did not release the conversation turn"
        time.sleep(0.2)
    assert stored["status"] in {"completed", "failed"}
    if stored["status"] == "failed":
        assert disconnect and stored["error_code"] == "interrupted"
    assert stored["tools"] == [proposal, confirmed]
    assert stored["inputs"][0]["id"] == input_row["id"]
    assert stored["inputs"][0]["status"] == "applied"
    return {"body": body, "proposal": proposal, "confirmed": confirmed,
            "turn": stored, "input": input_row, "disconnected": disconnect}


@pytest.fixture(scope="module")
def client():
    assert BASE in {"http://api:8000", "http://127.0.0.1:28000",
                    "http://127.0.0.1:28060", "http://127.0.0.1:28061"}, (
        "only the explicitly selected isolated local API is allowed")
    with httpx.Client(base_url=BASE, headers=ADMIN, trust_env=False,
                      follow_redirects=False, timeout=httpx.Timeout(75, connect=10)) as value:
        runtime = _json(value.get(f"{CONVERSATIONS}/runtime"))
        assert runtime["model_provider"] == "local", "simulation-only: never create enterprise candidates"
        assert {"propose_bundle_candidate", "start_data_loop", "evaluation_status"} <= set(runtime["available_tools"]), (
            "deploy the local trusted Prompt preset and Data Loop hooks before running this suite")
        yield value


@pytest.fixture(scope="module")
def flow(client):
    baseline = _json(client.get(f"{DATA_LOOP}/production-bundles/active"))["bundle"]
    assert baseline["status"] == "active"
    assert baseline["bundle_version"] == "local-simulation-bundle-v1", "only the synthetic local baseline is permitted"
    assert baseline["spec"]["analysis_prompt_version"] != PRESET
    conversation = _json(client.post(CONVERSATIONS,
        json={"title": f"simulation-only-conversation-dataloop-e2e-{uuid4()}"}), 201)
    cid = conversation["id"]

    def candidate_proposal(trace):
        result = trace["result"]
        assert result["confirmation"]["suggested"]["parameter"] == "analysis_prompt_version"
        assert result["confirmation"]["suggested"]["value"] == PRESET
        context = result["prepared_context"]
        assert context["base_bundle_id"] == baseline["id"]
        assert context["base_bundle_sha256"] == baseline["content_sha256"]
        assert context["base_bundle_version"] == baseline["bundle_version"]
        assert context["proposed_spec"]["analysis_prompt_version"] == PRESET
        assert any(item["asset"] == "analysis_prompt" and item["after_version"] == PRESET
                   for item in context["structured_diff"])
        fields = result["confirmation"]["fields"]
        assert any("人工批准" in field["value"] for field in fields)
        assert any(baseline["id"] == field["value"] for field in fields)

    candidate = _confirmed_stream(client, cid,
        {"request_id": str(uuid4()), "content": f"提交配置候选 analysis_prompt_version={PRESET}"},
        "propose_bundle_candidate", candidate_proposal)
    result = candidate["confirmed"]["result"]
    candidate_id = result["candidate"]["candidate_id"]
    UUID(candidate_id)
    assert result["receipt"]["entity_id"] == candidate_id
    assert result["receipt"]["pending_gate"] == "candidate_evaluation"
    assert result["receipt"]["status"] == "pending_approval"
    pending_candidate = _json(client.get(f"{DATA_LOOP}/configuration-candidates/{candidate_id}"))["candidate"]
    assert pending_candidate["status"] == "pending_evaluation"
    assert pending_candidate["base_bundle_id"] == baseline["id"]
    assert pending_candidate["content_sha256"] == result["receipt"]["version"]
    datasets = {}

    def evaluation_proposal(trace):
        context = trace["result"]["prepared_context"]
        assert trace["result"]["confirmation"]["suggested"] == {"candidate_id": candidate_id}
        assert context["candidate_id"] == candidate_id
        assert context["base_bundle_id"] == baseline["id"]
        assert context["base_bundle_sha256"] == baseline["content_sha256"]
        assert context["evaluation_policy_version"] == "hot-news-gate-v1"
        assert set(context["cohorts"]) == LAYERS
        fixture_case = uuid5(NAMESPACE_URL,
            f"NewsAgent:conversation-demo-fixture-v1:{HEADERS['X-Tenant-ID']}:case:fresh_bad_case")
        assert context["fresh_case_ids"] == [str(fixture_case)], "only the owned synthetic Fresh case is permitted"
        assert len({cohort["dataset_id"] for cohort in context["cohorts"].values()}) == 3
        assert datetime.fromisoformat(context["window_start"]) < datetime.fromisoformat(context["window_end"]) < datetime.now(timezone.utc)
        fields = trace["result"]["confirmation"]["fields"]
        displayed = "\n".join(field["value"] for field in fields)
        assert "新冻结" in displayed and "人工批准" in displayed
        for layer, cohort in context["cohorts"].items():
            dataset = _json(client.get(f"{DATA_LOOP}/datasets/{cohort['dataset_id']}"))["dataset"]
            assert dataset["tenant_id"] == HEADERS["X-Tenant-ID"]
            assert dataset["status"] == "frozen" and dataset["dataset_layer"] == layer
            # A previous execution may have frozen a newer Fresh template for
            # the same synthetic case. Its verified case ID permits replay;
            # Golden/risk must still come from the isolated seed namespace.
            assert dataset["dataset_name"].startswith("conversation-demo-fixture-v1") or (
                layer == "fresh_bad_case" and dataset["dataset_name"] == "conversation-fresh-bad-case")
            assert dataset["content_sha256"] == cohort["content_sha256"]
            assert dataset["dataset_version"] == cohort["dataset_version"]
            assert dataset["case_count"] == cohort["case_count"] >= 1
            assert cohort["dataset_id"] in displayed and cohort["content_sha256"] in displayed
            datasets[layer] = dataset

    evaluation = _confirmed_stream(client, cid,
        {"request_id": str(uuid4()), "content": f"启动候选 {candidate_id} 的评测"},
        "start_data_loop", evaluation_proposal, disconnect=True)
    _assert_accepted(evaluation["confirmed"]["result"])
    return {"conversation_id": cid, "baseline": baseline, "candidate": candidate,
            "candidate_id": candidate_id, "pending_candidate": pending_candidate,
            "evaluation": evaluation, "datasets": datasets}


@pytest.fixture(scope="module")
def evaluated(client, flow):
    workflow_id = flow["evaluation"]["confirmed"]["result"]["workflow_id"]
    url = f"{CONVERSATIONS}/receipts/evaluation-runs/{workflow_id}"
    deadline = time.monotonic() + 180
    last = None
    while time.monotonic() < deadline:
        response = client.get(url, headers=READ, timeout=15)
        if response.status_code == 503:
            last = {"receipt_http_status": 503}
        else:
            last = _json(response)
            assert last["workflow_id"] == workflow_id
            if last["workflow_accepted"] is True and last["run"]["phase"] in {"waiting_approval", "evaluation_failed"}:
                return last
            if last["workflow_accepted"] is False:
                _assert_accepted(last)
        time.sleep(3)
    pytest.fail("read-only waiting for business acceptance/evaluation timed out; inspect the worker and tenant admission "
                f"budget without approving or cancelling existing jobs. Last snapshot: {last}")


def test_candidate_confirmation_saves_actual_pending_candidate(flow):
    candidate = flow["pending_candidate"]
    assert candidate["status"] == "pending_evaluation"
    assert candidate["approved_evaluation_run_id"] is None
    assert candidate["proposed_spec"]["analysis_prompt_version"] == PRESET
    assert candidate["structured_diff"] == flow["candidate"]["confirmed"]["result"]["candidate"]["structured_diff"]


def test_confirmed_evaluation_uses_three_actual_frozen_cohorts_and_honest_acceptance(flow):
    assert set(flow["datasets"]) == LAYERS
    _assert_accepted(flow["evaluation"]["confirmed"]["result"])
    assert flow["evaluation"]["confirmed"]["result"]["run"]["evaluation_run_id"] is None


def test_sse_disconnect_and_uuid_replay_retain_same_committed_references(client, flow):
    assert flow["evaluation"]["disconnected"] is True
    cid = flow["conversation_id"]
    original_history = _history(client, cid)
    assert len(original_history["turns"]) == 2
    for item in (flow["candidate"], flow["evaluation"]):
        confirmation = item["proposal"]["result"]["confirmation"]
        url = f"{CONVERSATIONS}/{cid}/turns/{item['turn']['id']}/inputs"
        replayed_input = _json(client.post(url, json=_confirmation_body(confirmation)), 202)
        assert replayed_input["id"] == item["input"]["id"] and replayed_input["status"] == "applied"
        replay = _json(client.post(f"{CONVERSATIONS}/{cid}/messages", json=item["body"]))
        assert replay == item["turn"], "same UUID must return the saved turn instead of executing another write"
    assert _history(client, cid)["turns"] == original_history["turns"]


def test_business_evaluation_has_real_run_new_fresh_dataset_and_keeps_manual_gate(client, flow, evaluated):
    run, receipt = evaluated["run"], evaluated["receipt"]
    assert evaluated["workflow_accepted"] is True
    assert receipt["entity_id"] == evaluated["workflow_id"]
    assert receipt["deep_link"] == f"/data-loop?workflow_id={evaluated['workflow_id']}"
    assert re.fullmatch(r"[0-9a-f]{64}", receipt["version"])
    assert run["dataset_analysis_status"] in {"degraded", "disabled"}, (
        "seed cases without source runs cannot demonstrate complete source-based analysis")
    UUID(run["evaluation_run_id"])
    evaluation = _json(client.get(f"{DATA_LOOP}/evaluation-runs/{run['evaluation_run_id']}"))["evaluation"]
    assert evaluation["candidate_id"] == flow["candidate_id"]
    assert evaluation["base_bundle_id"] == flow["baseline"]["id"]
    assert evaluation["previous_experiment_candidate_id"] is None
    for layer in ("golden", "high_risk_regression"):
        assert evaluation["suite_metrics"][layer]["dataset_id"] == flow["datasets"][layer]["dataset_id"]
    fresh_id = evaluation["suite_metrics"]["fresh_bad_case"]["dataset_id"]
    assert fresh_id == run["dataset_id"] and fresh_id != flow["datasets"]["fresh_bad_case"]["dataset_id"]
    fresh = _json(client.get(f"{DATA_LOOP}/datasets/{fresh_id}"))["dataset"]
    assert fresh["status"] == "frozen" and fresh["dataset_name"] == "conversation-fresh-bad-case"
    assert fresh["case_count"] == flow["datasets"]["fresh_bad_case"]["case_count"]
    candidate = _json(client.get(f"{DATA_LOOP}/configuration-candidates/{flow['candidate_id']}"))["candidate"]
    assert candidate["approved_evaluation_run_id"] is None
    if run["phase"] == "waiting_approval":
        assert run["waiting_for_approval"] is True and run["gate_passed"] is True
        assert candidate["status"] == "evaluation_passed"
        assert receipt["status"] == "pending_approval" and receipt["pending_gate"] == "bundle_promotion"
    else:
        assert run["phase"] == "evaluation_failed"
        assert run["waiting_for_approval"] is False and run["gate_passed"] is False
        assert candidate["status"] == "evaluation_failed"
        assert receipt["status"] == "landed" and receipt["pending_gate"] is None
    assert _json(client.get(f"{DATA_LOOP}/production-bundles/active"))["bundle"] == flow["baseline"]


def _assert_tenant_scoping(client, flow, evaluated):
    other = {**READ, "X-Tenant-ID": str(uuid4())}
    paths = [f"{CONVERSATIONS}/{flow['conversation_id']}",
             f"{CONVERSATIONS}/receipts/evaluation-runs/{evaluated['workflow_id']}",
             f"{DATA_LOOP}/configuration-candidates/{flow['candidate_id']}",
             f"{DATA_LOOP}/evaluation-runs/{evaluated['run']['evaluation_run_id']}"]
    for path in paths:
        assert client.get(path, headers=other).status_code == 404, path
    for dataset in flow["datasets"].values():
        response = client.get(f"{DATA_LOOP}/datasets/{dataset['dataset_id']}", headers=other)
        # The existing dataset API maps its not-ready/not-found exception to
        # 409. Preserve that contract while proving it returns no tenant data.
        assert response.status_code == 409
        body = response.json()
        assert isinstance(body, dict) and "dataset" not in body
        assert set(body) == {"detail"} and body["detail"] == "evaluation dataset not found"
    receipt_url = f"{CONVERSATIONS}/receipts/evaluation-runs/{evaluated['workflow_id']}"
    # Client defaults contain admin roles: explicitly clear that header when
    # checking absent or incorrectly grouped gateway permissions.
    denied = {**HEADERS, "X-Data-Loop-Roles": ""}
    assert client.get(receipt_url, headers=denied).status_code == 403
    assert client.get(receipt_url, headers={**denied, "X-Writing-Roles": "data-loop:admin"}).status_code == 403
    assert _json(client.get(receipt_url, headers=READ))["workflow_id"] == evaluated["workflow_id"]


def test_readonly_receipt_and_entities_are_tenant_scoped(client, flow, evaluated):
    _assert_tenant_scoping(client, flow, evaluated)


@pytest.mark.skipif(not EXISTING_CONVERSATION_ID,
                    reason="set an existing synthetic conversation UUID for the GET-only recheck")
def test_existing_receipt_entities_are_tenant_scoped_readonly(client):
    cid = str(UUID(EXISTING_CONVERSATION_ID))
    history = _json(client.get(f"{CONVERSATIONS}/{cid}", headers=READ))
    assert history["conversation"]["title"].startswith("simulation-only-conversation-dataloop-e2e-")
    traces = [trace for turn in history["turns"] for trace in turn["tools"]]
    candidate_result = next(trace["result"] for trace in traces
        if trace["name"] == "propose_bundle_candidate" and trace["result"].get("candidate"))
    evaluation_result = next(trace["result"] for trace in traces
        if trace["name"] == "start_data_loop" and trace["result"].get("workflow_id"))
    evaluation_proposal = next(trace["result"]["prepared_context"] for trace in traces
        if trace["name"] == "start_data_loop" and trace["result"].get("confirmation"))
    workflow_id = evaluation_result["workflow_id"]
    assert WORKFLOW_ID.fullmatch(workflow_id)
    current = _json(client.get(f"{CONVERSATIONS}/receipts/evaluation-runs/{workflow_id}", headers=READ))
    assert current["workflow_accepted"] is True
    assert current["run"]["phase"] in {"waiting_approval", "evaluation_failed"}
    datasets = {}
    for layer, cohort in evaluation_proposal["cohorts"].items():
        dataset = _json(client.get(f"{DATA_LOOP}/datasets/{cohort['dataset_id']}", headers=READ))["dataset"]
        assert dataset["content_sha256"] == cohort["content_sha256"]
        datasets[layer] = dataset
    assert set(datasets) == LAYERS
    fresh_id = current["run"]["dataset_id"]
    datasets["evaluated_fresh"] = _json(client.get(f"{DATA_LOOP}/datasets/{fresh_id}", headers=READ))["dataset"]
    candidate_id = candidate_result["candidate"]["candidate_id"]
    candidate = _json(client.get(f"{DATA_LOOP}/configuration-candidates/{candidate_id}", headers=READ))["candidate"]
    assert candidate["status"] in {"evaluation_passed", "evaluation_failed"}
    assert candidate["approved_evaluation_run_id"] is None
    evaluation = _json(client.get(f"{DATA_LOOP}/evaluation-runs/{current['run']['evaluation_run_id']}", headers=READ))["evaluation"]
    assert evaluation["candidate_id"] == candidate_id
    baseline = _json(client.get(f"{DATA_LOOP}/production-bundles/active", headers=READ))["bundle"]
    assert baseline["id"] == candidate["base_bundle_id"] and baseline["bundle_version"] == "local-simulation-bundle-v1"
    _assert_tenant_scoping(client, {"conversation_id": cid, "candidate_id": candidate_id, "datasets": datasets}, current)
