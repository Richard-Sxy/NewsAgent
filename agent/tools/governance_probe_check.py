"""Run the opt-in cross-process diagnostic and save sanitized trace evidence."""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
from uuid import uuid4

import httpx

from tools.governance_load import collect_traces, local_url


async def run(args):
    # The dedicated API URL is fixed to the local diagnostic service.
    if args.api not in {"http://127.0.0.1:38103","http://probe-api:8000"}:
        raise ValueError("isolated diagnostic API required")
    tempo = local_url(args.tempo)
    request_id, trace_id = str(uuid4()), secrets.token_hex(16)
    async with httpx.AsyncClient(timeout=40,trust_env=False) as client:
        response = await client.post(args.api+"/probe/"+request_id,
            headers={"traceparent":f"00-{trace_id}-{secrets.token_hex(8)}-01"})
        response.raise_for_status()
        body = response.json()
        sample = {"trace_id":response.headers.get("x-trace-id")}
        await collect_traces(client,tempo,[sample])
    spans = sample.get("spans",[])
    names = [s["name"] for s in spans]
    checks = {"completed":body.get("status") == "completed", "parent_preserved":sample["trace_id"] == trace_id,
        "http":any(n.startswith("POST ") for n in names),
        "workflow":any(n.startswith("RunWorkflow:") for n in names),
        "activity":"temporal.activity" in names,"model":"model.structured" in names,
        "quota":"model.quota_wait.interactive" in names,"business_validation":"model.business_validate" in names,
        "two_services":{s["service"] for s in spans} == {"newsagent-governance-probe-api","newsagent-governance-probe-worker"}}
    report = {"synthetic_only":True,"request_id":request_id,"workflow_id":body.get("workflow_id"),
              "model_request_id":body.get("model_request_id"),"checks":checks,**sample,"passed":all(checks.values())}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k != "spans"}),flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api",default="http://127.0.0.1:38103")
    p.add_argument("--tempo",default="http://127.0.0.1:23200")
    p.add_argument("--output",type=Path,required=True)
    raise SystemExit(asyncio.run(run(p.parse_args())))
