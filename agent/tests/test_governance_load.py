import argparse
import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest

from tools.governance_load import correct_turn, local_url, trace_summary, run, collect_traces


def turn():
    return {"status":"completed","request_id":"request","user_content":"test",
            "assistant_content":"answer","model_request_ids":["model"],
            "tools":[{"name":"list_hot_news","status":"completed","error_code":None}]}


@pytest.mark.parametrize("patch", [
    {"status":"processing"},{"request_id":"wrong"},{"user_content":"other"},
    {"assistant_content":""},{"model_request_ids":[]},{"tools":[]},
    {"tools":[{"name":"list_hot_news","status":"failed"}]},
    {"tools":[{"name":"list_hot_news","status":"completed","error_code":"degraded"}]},
])
def test_200_or_completed_alone_is_not_business_correct(patch):
    assert not correct_turn({**turn(),**patch},"request","test","existing_reports")


def test_valid_contract_and_unknown_tokens():
    assert correct_turn(turn(),"request","test","existing_reports")
    assert trace_summary({}) == []
    spans = trace_summary({"batches":[{"resource":{"attributes":[]},"scopeSpans":[{"spans":[
        {"name":"model.structured","startTimeUnixNano":"1000000000","endTimeUnixNano":"2000000000",
         "attributes":[{"key":"prompt","value":{"stringValue":"PRIVATE"}}]}]}]}]})
    assert spans[0]["seconds"] == 1
    assert spans[0]["attributes"] == {}
    assert "PRIVATE" not in str(spans)


@pytest.mark.parametrize("url",["https://enterprise.example","http://user:pass@localhost","http://localhost?x=y","http://localhost/private"])
def test_target_validation(url):
    with pytest.raises(ValueError):
        local_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None,"business","persistence","setup","trace"])
async def test_soak_denominator_checks_and_trace_gate(monkeypatch,tmp_path,fault):
    records = {}
    async def handle(request):
        path=request.url.path
        owner=(request.headers["X-Tenant-ID"],request.headers["X-User-ID"])
        if path.endswith("/runtime"):
            return httpx.Response(200,json={"model_provider":"local","model_route":"test"})
        if path == "/api/v1/conversations":
            if fault == "setup":
                return httpx.Response(503)
            cid=str(uuid4())
            records[cid]={"owner":owner,"turns":[]}
            return httpx.Response(201,json={"id":cid})
        cid=path.split("/")[4]
        record=records[cid]
        if owner != record["owner"]:
            return httpx.Response(404)
        if request.method == "GET":
            return httpx.Response(200,json={"turns":[] if fault == "persistence" else record["turns"]})
        payload=json.loads(request.content)
        if not record["turns"]:
            record["turns"].append({**turn(),"id":str(uuid4()),"request_id":payload["request_id"],
                "user_content":payload["content"],"status":"failed" if fault == "business" else "completed"})
        parent=request.headers.get("traceparent","00-unknown-0-01")
        return httpx.Response(200,json=record["turns"][0],headers={"x-trace-id":parent.split("-")[1]})
    original=httpx.AsyncClient
    monkeypatch.setattr("tools.governance_load.httpx.AsyncClient",lambda **kw:original(**kw,transport=httpx.MockTransport(handle)))
    async def traces(client,tempo,samples):
        for s in samples:
            s["trace_status"]="found" if s.get("trace_id") and fault != "trace" else "missing"
    monkeypatch.setattr("tools.governance_load.collect_traces",traces)
    output=tmp_path/"result.json"
    result=await run(argparse.Namespace(base_urls=["http://api-a:8000","http://api-b:8000"],
        tempo="http://gateway:3200",users=2,duration=.03,think_seconds=.01,timeout=1,output=output))
    report=json.loads(output.read_text())
    assert report["attempts"] > 0
    journal=(tmp_path/report["sample_journal"]).read_text().splitlines()
    assert len(journal)==report["attempts"]
    assert all("assistant_content" not in row and "user_content" not in row for row in journal)
    assert result == (0 if fault is None else 1)
    assert report["business_correct"] == (report["attempts"] if fault in {None,"trace"} else 0)
    if fault == "setup":
        assert report["latency_seconds_all"] is None


@pytest.mark.asyncio
async def test_trace_readback_has_a_total_budget_including_queued_requests():
    class Slow:
        async def get(self,*args,**kwargs):
            await asyncio.sleep(10)
    samples=[{"trace_id":"a"*32} for _ in range(10)]
    started=time.monotonic()
    await collect_traces(Slow(),"http://gateway:3200",samples,max_seconds=.03)
    assert time.monotonic()-started < .5
    assert all(s["trace_status"]=="readback_budget_exhausted" for s in samples)


@pytest.mark.asyncio
async def test_trace_query_uses_the_measured_time_window():
    def handler(request):
        assert dict(request.url.params)=={"start":"40","end":"161"}
        return httpx.Response(200,json={"batches":[{"scopeSpans":[{"spans":[{"name":"model.structured"}]}]}]})
    samples=[{"trace_id":"a"*32,"started_at_epoch":100,"ended_at_epoch":101}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await collect_traces(client,"http://gateway:3200",samples,max_seconds=1)
    assert samples[0]["trace_status"]=="found"
