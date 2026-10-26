"""Bounded local two-replica soak, business checks and per-question trace evidence.

Fresh synthetic conversations avoid growing context during measurement. This is
closed-loop virtual-user traffic with explicit think time, not constant arrival rate.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from tools.conversation_load import distribution, LOCAL_TOKEN, PREFIX, TENANT


def local_url(value):
    parsed = urlparse(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "api-a", "api-b", "gateway"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("isolated local base URL required")
    return value.rstrip("/")


def correct_turn(turn, request_id, content, workload):
    if not isinstance(turn, dict):
        return False
    tools = turn.get("tools", [])
    return (turn.get("status") == "completed" and turn.get("request_id") == request_id
            and turn.get("user_content") == content and bool(turn.get("assistant_content"))
            and bool(turn.get("model_request_ids"))
            and all(t.get("status") == "completed" and not t.get("error_code") for t in tools)
            and (workload == "greeting" or any(t.get("name") == "list_hot_news" for t in tools)))


def trace_summary(document):
    result = []
    for batch in document.get("batches", document.get("resourceSpans", [])):
        resource = {a["key"]: a["value"].get("stringValue") for a in batch.get("resource", {}).get("attributes", [])}
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            for span in scope.get("spans", []):
                attrs = {a["key"]: next(iter(a["value"].values()), None) for a in span.get("attributes", [])}
                result.append({"name": span["name"], "span_id": span.get("spanId"),
                    "parent_span_id": span.get("parentSpanId"), "service": resource.get("service.name"),
                    "instance": resource.get("service.instance.id"),
                    "version": resource.get("service.version"), "status": span.get("status", {}).get("code", 0),
                    "seconds": (int(span.get("endTimeUnixNano", 0))-int(span.get("startTimeUnixNano", 0)))/1e9,
                    "attributes": {k:v for k,v in attrs.items() if k in {
                        "error.stage", "error.kind", "error.type", "newsagent.stage", "model.scene",
                        "model.provider_request_id", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
                        "gen_ai.usage.total_tokens", "temporal.workflow.id", "temporal.activity.attempt"}}})
    return result


async def collect_traces(client, tempo, samples, *, max_seconds=120):
    semaphore = asyncio.Semaphore(1)
    async def one(sample):
        if not sample.get("trace_id"):
            sample["trace_status"] = "missing_response_trace_id"
            return
        async with semaphore:
            try:
                # Without a time window Tempo scans every historical block.
                window = {"start":int(sample.get("started_at_epoch",time.time()-3600))-60,
                          "end":int(sample.get("ended_at_epoch",time.time()))+60}
                response = await client.get(tempo+"/api/traces/"+sample["trace_id"],
                    params=window,headers={"Accept": "application/json"},timeout=20)
                if response.status_code == 200:
                    spans = trace_summary(response.json())
                    if spans and any(s["name"] == "model.structured" for s in spans):
                        sample["trace_status"], sample["spans"] = "found", spans
                        return
                sample["trace_status"] = "not_found_or_incomplete"
            except (httpx.HTTPError, ValueError, KeyError):
                sample["trace_status"] = "query_failed"
    try:
        async with asyncio.timeout(max_seconds):
            while True:
                missing = [s for s in samples if s.get("trace_id") and s.get("trace_status") != "found"]
                if not missing:
                    break
                await asyncio.gather(*(one(s) for s in missing))
                # A bulk retry loop can amplify a slow backend into ingestion
                # failures. Resume explicitly after inspecting backend health.
                if len(samples)>32 or all(s.get("trace_status") == "found" for s in missing):
                    break
                await asyncio.sleep(2)
    except TimeoutError:
        for sample in samples:
            if sample.get("trace_id") and sample.get("trace_status") != "found":
                sample["trace_status"] = "readback_budget_exhausted"
    for sample in samples:
        if not sample.get("trace_id"):
            sample["trace_status"] = "missing_response_trace_id"


async def run(args):
    bases = [local_url(x) for x in args.base_urls]
    tempo = local_url(args.tempo)
    samples, runtime = [], []
    run_id = uuid4().hex
    args.output.parent.mkdir(parents=True,exist_ok=True)
    journal = args.output.with_name(args.output.stem+"."+run_id+".samples.jsonl")
    headers = {"Authorization": "Bearer "+LOCAL_TOKEN, "X-Tenant-ID": TENANT, "X-Hot-News-Roles": "hot-news:read"}
    limits = httpx.Limits(max_connections=args.users*3+10, max_keepalive_connections=args.users*2)
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits, trust_env=False) as client:
        for base in bases:
            response = await client.get(base+PREFIX+"/runtime", headers={**headers,"X-User-ID":str(uuid4())})
            response.raise_for_status()
            info = response.json()
            if info.get("model_provider") != "local":
                raise ValueError("this acceptance run requires isolated deterministic model Ports")
            runtime.append({"base_url":base, "model_provider":info["model_provider"], "model_route":info["model_route"],
                            "prompt_version":info.get("prompt_version")})
        began = time.monotonic()
        deadline = began+args.duration
        active = peak = 0

        async def user(index):
            nonlocal active, peak
            count = 0
            while time.monotonic() < deadline:
                base = bases[(index+count)%len(bases)]
                other_base = bases[(index+count+1)%len(bases)]
                identity = {**headers, "X-User-ID":str(uuid4())}
                workload = "greeting" if index%2 == 0 else "existing_reports"
                content = "你好" if workload == "greeting" else "读取已完成的热点报告，使用 list_hot_news 和 read_hot_news，不创建新查询。"
                request_id, trace_id = str(uuid4()), secrets.token_hex(16)
                sample = {"user_index":index,"sequence":count,"replica":base,"workload":workload,
                          "started_at_epoch":time.time(),
                          "request_id":request_id,"status":"failed","business_correct":False,
                          "history":False,"replay":False,"isolation":False}
                started = None
                try:
                    create = await client.post(base+PREFIX, headers=identity, json={"title":"governance-"+run_id[:8]})
                    create.raise_for_status()
                    cid = create.json()["id"]
                    sample["conversation_id"] = cid
                    payload = {"request_id":request_id,"content":content}
                    sampled = {**identity,"traceparent":f"00-{trace_id}-{secrets.token_hex(8)}-01"}
                    started = time.monotonic()
                    sample["sent_trace_id"] = trace_id
                    active += 1
                    peak = max(peak, active)
                    try:
                        response = await client.post(base+PREFIX+f"/{cid}/messages",headers=sampled,json=payload)
                    finally:
                        sample["seconds"] = time.monotonic()-started
                        active -= 1
                    sample["http_status"] = response.status_code
                    sample["trace_id"] = response.headers.get("x-trace-id")
                    sample["trace_parent_preserved"] = sample["trace_id"] == trace_id
                    response.raise_for_status()
                    turn = response.json()
                    sample.update(status=turn.get("status"),turn_id=turn.get("id"),
                        error_code=turn.get("error_code"), model_request_ids=turn.get("model_request_ids",[]),
                        tools=[{"name":t.get("name"),"status":t.get("status"),"error_code":t.get("error_code")} for t in turn.get("tools",[])])
                    sample["business_correct"] = correct_turn(turn,request_id,content,workload)
                    # Persistence and same-UUID replay are checked through the OTHER replica.
                    history = await client.get(other_base+PREFIX+f"/{cid}",headers=identity)
                    sample["history"] = history.status_code == 200 and history.json().get("turns") == [turn]
                    replay = await client.post(other_base+PREFIX+f"/{cid}/messages",headers=identity,json=payload)
                    sample["replay"] = replay.status_code == 200 and replay.json() == turn
                    outsider = await client.get(other_base+PREFIX+f"/{cid}",headers={**identity,"X-User-ID":str(uuid4())})
                    tenant = await client.get(other_base+PREFIX+f"/{cid}",headers={**identity,"X-Tenant-ID":str(uuid4())})
                    sample["isolation"] = outsider.status_code == tenant.status_code == 404
                except (httpx.HTTPError, TimeoutError, ValueError, KeyError) as error:
                    sample["check_error"] = type(error).__name__
                sample["ended_at_epoch"] = time.time()
                if not sample.get("trace_id") and sample.get("sent_trace_id"):
                    sample["trace_id"] = sample["sent_trace_id"]
                # Preserve measured samples before expensive readback or interruption.
                with journal.open("a",encoding="utf-8") as handle:
                    handle.write(json.dumps(sample,ensure_ascii=False)+"\n")
                samples.append(sample)
                count += 1
                if time.monotonic() < deadline:
                    await asyncio.sleep(min(args.think_seconds,max(0,deadline-time.monotonic())))

        tasks = [asyncio.create_task(user(i)) for i in range(args.users)]
        async def progress():
            while not all(t.done() for t in tasks):
                await asyncio.sleep(10)
                print(json.dumps({"elapsed":round(time.monotonic()-began,1),"attempts":len(samples),
                    "business_correct":sum(s["business_correct"] for s in samples)}),flush=True)
        logger = asyncio.create_task(progress())
        await asyncio.gather(*tasks)
        logger.cancel()
        await asyncio.gather(logger,return_exceptions=True)
        elapsed = time.monotonic()-began
        await collect_traces(client,tempo,samples)
    correct = sum(all(s.get(k) for k in ("business_correct","history","replay","isolation")) for s in samples)
    report = {"run_id":run_id,"created_at":datetime.now(timezone.utc).isoformat(),"runtime":runtime,
        "users":args.users,"duration_requested_seconds":args.duration,"duration_actual_seconds":round(elapsed,3),
        "think_seconds":args.think_seconds,"client_peak_inflight":peak,"attempts":len(samples),
        "sample_journal":journal.name,
        "business_correct":correct,"business_correct_pct":round(100*correct/len(samples),4) if samples else None,
        "latency_seconds_all":distribution([s["seconds"] for s in samples if "seconds" in s]),
        "latency_seconds_correct":distribution([s["seconds"] for s in samples if s.get("business_correct")]),
        "trace_found":sum(s.get("trace_status") == "found" for s in samples),
        "statuses":dict(Counter(s["status"] for s in samples)),"samples":samples,
        "limitations":["local deterministic model; no enterprise quality or capacity claim",
            "10 virtual users does not mean 10 simultaneous model calls; think time and actual peak reported",
            "JSON full-turn latency, not raw token TTFT; fresh conversations, no compaction",
            "read-only existing report workload; no new hot-news/write/data-loop workflow in this soak",
            "missing provider token usage remains unknown; no synthetic token counts",
            "model admission covers native inference factory; embedding and direct custom Ports are excluded"]}
    report["passed"] = bool(samples) and correct == len(samples) and report["trace_found"] == len(samples) and all(s.get("trace_parent_preserved") for s in samples)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in {"samples","limitations","runtime"}},ensure_ascii=False),flush=True)
    return 0 if report["passed"] else 1


async def recheck(args):
    """Read-only resume; write a NEW report, preserving the original observations."""
    if args.output.resolve() == args.recheck_report.resolve():
        raise ValueError("readback output must differ from original report")
    report=json.loads(args.recheck_report.read_text(encoding="utf-8"))
    async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
        await collect_traces(client,local_url(args.tempo),report["samples"])
    report["trace_found"]=sum(s.get("trace_status")=="found" for s in report["samples"])
    report["trace_expected"]=sum(bool(s.get("trace_id")) for s in report["samples"])
    report["readback_of"]=args.recheck_report.name
    # Never turn a failed business load into a pass just by repairing readback.
    report["passed"]=(report["business_correct"]==report["attempts"]
        and report["trace_found"]==report["attempts"]
        and all(s.get("trace_parent_preserved") for s in report["samples"]))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:report[k] for k in ("attempts","business_correct","trace_found","trace_expected","passed")}),flush=True)
    return 0 if report["trace_found"]==report["trace_expected"] else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-urls", nargs="+", default=["http://127.0.0.1:38101","http://127.0.0.1:38102"])
    p.add_argument("--tempo",default="http://127.0.0.1:23200")
    p.add_argument("--users",type=int,default=10)
    p.add_argument("--duration",type=float,default=600)
    p.add_argument("--think-seconds",type=float,default=5)
    p.add_argument("--timeout",type=float,default=30)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--recheck-report",type=Path)
    args = p.parse_args()
    if not (1<=args.users<=100 and 1<=args.duration<=3600 and .01<=args.think_seconds<=60 and 1<=args.timeout<=150):
        p.error("bounded users/duration/think/timeout required")
    raise SystemExit(asyncio.run(recheck(args) if args.recheck_report else run(args)))


if __name__ == "__main__":
    main()
