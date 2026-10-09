"""Verify load accounting without calling any model or database."""
import argparse
import json
from uuid import uuid4

import httpx
import pytest

from tools.conversation_load import distribution, run


@pytest.mark.asyncio
@pytest.mark.parametrize("transport,business_status,degraded", [
    ("json", "completed", False), ("sse", "completed", False),
    ("sse", "failed", False), ("sse", "completed", True),
])
async def test_load_verifies_persisted_turns_and_counts_business_failures(monkeypatch, tmp_path, transport, business_status, degraded):
    records = {}

    async def handle(request):
        path = request.url.path
        owner = (request.headers["X-Tenant-ID"], request.headers["X-User-ID"])
        if path.endswith("/runtime"):
            return httpx.Response(200, json={"model_provider": "local", "model_route": "test"})
        if path == "/api/v1/conversations":
            conversation = str(uuid4())
            records[conversation] = {"owner": owner, "turns": []}
            return httpx.Response(201, json={"id": conversation})
        conversation = path.split("/")[4]
        record = records[conversation]
        if record["owner"] != owner:
            return httpx.Response(404)
        if request.method == "GET":
            return httpx.Response(200, json={"turns": record["turns"]})
        payload = json.loads(request.content)
        turn = next((t for t in record["turns"] if t["request_id"] == payload["request_id"]), None)
        if turn is None:
            turn = {"id": str(uuid4()), "request_id": payload["request_id"], "user_content": payload["content"],
                    "status": business_status, "error_code": "model_timeout" if business_status == "failed" else None,
                    "model_request_ids": ["test-model-request"],
                    "tools": [{"status": "denied", "error_code": "tool_denied"}] if degraded else []}
            record["turns"].append(turn)
        if path.endswith("/stream"):
            body = f'event: accepted\ndata: {{}}\n\n: heartbeat\n\nevent: answer_delta\ndata: {{"text":"答复"}}\n\nevent: done\ndata: {json.dumps({"turn": turn})}\n\n'
            return httpx.Response(200, content=body.encode(), headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=turn)

    original = httpx.AsyncClient
    monkeypatch.setattr("tools.conversation_load.httpx.AsyncClient",
                        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)))
    output = tmp_path / "report.json"
    args = argparse.Namespace(base_url="http://127.0.0.1:28000", users=2, rounds=2,
                              transport=transport, workload="mixed", timeout=5, output=output)
    result = await run(args)
    report = json.loads(output.read_text())
    assert report["attempted_turns"] == 4
    assert report["completed_turns"] == (4 if business_status == "completed" else 0)
    assert report["clean_completed_turns"] == (4 if business_status == "completed" and not degraded else 0)
    assert result == (0 if business_status == "completed" and not degraded else 1)
    assert report["verification_passes"] == dict.fromkeys(
        ("history", "replay", "other_user_denied", "other_tenant_denied"), 2)
    assert sum(len(r["turns"]) for r in records.values()) == 4, "replay must not create turns"
    assert bool(report["first_event_seconds"]) == (transport == "sse")


def test_percentiles_use_nearest_rank_and_empty_samples_are_explicit():
    assert distribution([]) is None
    assert distribution([1, 2, 3, 4]) == {"p50": 2, "p95": 4, "p99": 4, "max": 4}


@pytest.mark.asyncio
async def test_production_target_is_rejected_before_network_access():
    with pytest.raises(ValueError, match="isolated"):
        await run(argparse.Namespace(base_url="https://enterprise.example"))
