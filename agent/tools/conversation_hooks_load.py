"""Two synthetic tenants exercise confirmed writing admission through real HTTP.

Run only with ENVIRONMENT=e2e against the local API and local model. Each tenant
creates three conversations and confirms three writing proposals concurrently.
Reports retain aggregate timings and entity references, never message text,
credentials or confirmation tokens. Human writing gates remain untouched.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from uuid import UUID, uuid4

import httpx


PREFIX = "/api/v1/conversations"
ALLOWED_URLS = {"http://127.0.0.1:28060", "http://127.0.0.1:28000", "http://api:8000"}
LOCAL_TOKEN = "newsagent-native-local-token-not-for-production"
TEST_CONTENT = "写一篇本地并发验收新闻简报，要求简洁表达"
REQUEST_ID = re.compile(r"^conversation-admission-request-[0-9a-f]{64}$")


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:28060")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--observe-seconds", type=float, default=15)
    parser.add_argument("--poll-interval", type=float, default=3)
    parser.add_argument("--token-env", default="NEWS_AGENT_HOOKS_LOAD_TOKEN")
    parser.add_argument("--resource-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def validate_args(args):
    if os.environ.get("ENVIRONMENT") != "e2e":
        raise ValueError("ENVIRONMENT=e2e is required")
    if args.base_url not in ALLOWED_URLS:
        raise ValueError("Only isolated local API URLs are accepted")
    if not 5 <= args.timeout <= 120 or not 3 <= args.observe_seconds <= 120 or not .1 <= args.poll_interval <= 3:
        raise ValueError("timeout 5..120, observation 3..120, poll interval 0.1..3 required")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", args.token_env):
        raise ValueError("token environment variable name is invalid")
    for path in (args.output, args.resource_report):
        if path is not None and (path.suffix.lower() != ".json" or path.name.startswith(".env")
                                 or path.resolve().name.startswith(".env")):
            raise ValueError("Reports must be JSON files, never environment files")


def percentiles(values):
    ordered = sorted(values)
    return ({name: round(ordered[max(0, math.ceil(len(ordered) * rank) - 1)], 3)
             for name, rank in (("p50", .5), ("p95", .95), ("max", 1))} if ordered else None)


def resource_summary(path):
    if path is None:
        return None
    if path.stat().st_size > 1048576:
        raise ValueError("Resource report exceeds the bounded JSON size")
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value if isinstance(value, list) else value.get("containers", []) if isinstance(value, dict) else []
    safe = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        name = row.get("Name", row.get("name", ""))
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name) is None:
            continue
        item = {"name": name}
        for field, output in (("CPUPercent", "cpu_percent"), ("MemPerc", "memory_percent")):
            raw = row.get(field, row.get("CPUPerc") if field == "CPUPercent" else None)
            if isinstance(raw, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%", raw):
                number = float(raw[:-1])
                if math.isfinite(number):
                    item[output] = number
        raw = row.get("PIDs")
        if isinstance(raw, str) and re.fullmatch(r"[0-9]{1,9}", raw):
            item["pids"] = int(raw)
        raw = row.get("MemUsage")
        if isinstance(raw, str) and re.fullmatch(r"[0-9.]+\s*[kMGTPE]?i?B\s*/\s*[0-9.]+\s*[kMGTPE]?i?B", raw):
            item["memory_usage"] = raw
        safe.append(item)
    return {"containers": safe, "scope": "external Docker aggregate snapshot; not continuous peak sampling"}


async def sse_events(response):
    name, data, size = "message", [], 0
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:].lstrip()
            size += len(value)
            if size > 262144:
                raise ValueError("SSE event exceeds the bounded payload size")
            data.append(value)
        elif not line and data:
            yield name, json.loads("\n".join(data))
            name, data, size = "message", [], 0


def admission_snapshot(result):
    source = result.get("admission")
    if not isinstance(source, dict) or not REQUEST_ID.fullmatch(str(source.get("request_workflow_id", ""))):
        raise ValueError("Admission snapshot has no valid server request reference")
    count, budget, position = source.get("active_count"), source.get("budget"), source.get("queue_position")
    if count is not None and (type(count) is not int or count < 0):
        raise ValueError("Admission active count is invalid")
    if type(budget) is not int or budget != 2:
        raise ValueError("The load scenario requires the authoritative budget of two")
    if position is not None and (type(position) is not int or position < 1):
        raise ValueError("Admission queue position is invalid")
    state = source.get("state")
    if state not in {"submitted", "queued", "granted", "starting", "running", "unknown", "terminal", "rejected"}:
        raise ValueError("Admission state is invalid")
    if state == "queued" and (position is None or result.get("workflow_accepted") is not False):
        raise ValueError("Queued admission must not claim a started business Workflow")
    if type(result.get("workflow_accepted")) is not bool or type(result.get("admission_accepted")) is not bool:
        raise ValueError("Admission acceptance flags must be authoritative booleans")
    if state != "rejected" and result["admission_accepted"] is not True:
        raise ValueError("The request has not been durably admitted")
    return {"state": state, "queue_position": position, "active_count": count, "budget": budget,
            "request_workflow_id": source["request_workflow_id"],
            "workflow_accepted": result.get("workflow_accepted"),
            "admission_accepted": result.get("admission_accepted")}


def build_report(*, run_id, base_url, samples, observations, elapsed, resources=None):
    tenants = []
    failures = []
    for tenant_index in range(2):
        written = [sample for sample in samples if sample["tenant_index"] == tenant_index]
        read = [item for item in observations if item["tenant_index"] == tenant_index]
        snapshots = [item["admission"] for item in [*written, *read] if item.get("admission")]
        counts = [item["active_count"] for item in snapshots if item["active_count"] is not None]
        queued = [item for item in snapshots if item["state"] == "queued"]
        references = {item["request_workflow_id"] for item in snapshots}
        monotonic = True
        for reference in references:
            positions = [item["queue_position"] for item in snapshots
                         if item["request_workflow_id"] == reference and item["state"] == "queued"]
            monotonic = monotonic and all(later <= earlier for earlier, later in zip(positions, positions[1:]))
        checks = {"three_confirmed_requests": len(written) == 3 and all(item["outcome"] == "completed"
                and item.get("confirmation_accepted") is True for item in written),
            "authoritative_count_observed": bool(counts), "budget_not_exceeded": bool(counts) and max(counts) <= 2,
            "queue_observed": bool(queued), "distinct_admission_references": len(references) == 3,
            "all_receipts_observed": len({item["job_id"] for item in read if item.get("admission")}) == 3,
            "fifo_queue_positions_nonincreasing": monotonic and bool(queued)}
        failures.extend(f"tenant_{tenant_index}:{name}" for name, passed in checks.items() if not passed)
        tenants.append({"tenant_index": tenant_index, "peak_observed_active_count": max(counts) if counts else None,
            "queued_samples": len(queued), "verification": checks,
            "queue_references": [{"request_workflow_id": item["request_workflow_id"], "queue_position": item["queue_position"]}
                for item in queued], "admission_states": dict(Counter(item["state"] for item in snapshots))})
    return {"run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(), "base_url": base_url,
        "scope": "conversation_async_hooks", "tenants": tenants, "attempted_requests": len(samples),
        "confirmed_requests": sum(item.get("confirmation_accepted", False) for item in samples),
        "completed_turns": sum(item["outcome"] == "completed" for item in samples),
        "elapsed_seconds": round(elapsed, 3), "turn_latency_seconds": percentiles([item["seconds"] for item in samples]),
        "confirmation_latency_seconds": percentiles([item["confirmation_seconds"] for item in samples if item.get("confirmation_seconds") is not None]),
        "verification_passed": not failures, "failed_or_inconclusive_checks": failures,
        "samples": samples, "receipt_observations": observations, "resource_snapshot": resources,
        "generated_test_data_retained": True,
        "limitations": ["Local rule simulation does not establish enterprise model quality.",
            "Budget covers conversation async hooks; page/API and legacy workflows are outside this claim.",
            "FIFO checks reference order and nonincreasing positions; this run does not approve human gates to drain the queue.",
            "Fast terminal workflows can make queue evidence inconclusive; absent evidence never counts as passing.",
            "Observed concurrency and optional resource snapshot do not establish sustained production capacity."]}


async def run(args):
    validate_args(args)
    resources = resource_summary(args.resource_report)
    token = os.environ.get(args.token_env, LOCAL_TOKEN)
    if not token or any(character.isspace() for character in token):
        raise ValueError("Local gateway credential is unavailable")
    run_id = uuid4().hex
    identities = [(str(uuid4()), [str(uuid4()) for _ in range(3)]) for _ in range(2)]
    headers = {"Authorization": f"Bearer {token}", "X-Hot-News-Roles": "hot-news:read", "X-Writing-Roles": "writing:admin"}
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=args.timeout,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=12), trust_env=False, follow_redirects=False) as client:
        probe = {"X-Tenant-ID": identities[0][0], "X-User-ID": identities[0][1][0]}
        response = await client.get(PREFIX + "/runtime", headers=probe)
        response.raise_for_status()
        runtime = response.json()
        budget = runtime.get("async_admission", {})
        if (runtime.get("model_provider") != "local" or "start_writing_intent" not in runtime.get("available_tools", [])
                or budget.get("enabled") is not True or budget.get("budget") != 2
                or budget.get("scope") != "conversation_async_hooks"):
            raise ValueError("Runtime must be local with explicitly enabled conversation admission budget two")
        conversations = []
        for tenant_index, (tenant, users) in enumerate(identities):
            for user_index, user in enumerate(users):
                owner = {"X-Tenant-ID": tenant, "X-User-ID": user}
                response = await client.post(PREFIX, headers=owner, json={"title": f"hooks-load-{run_id[:8]}"})
                response.raise_for_status()
                conversations.append((tenant_index, user_index, owner, str(UUID(response.json()["id"]))))
        samples, observations = [], []
        gate = asyncio.Event()
        began = time.perf_counter()

        async def send(identity):
            tenant_index, user_index, owner, conversation = identity
            sample = {"tenant_index": tenant_index, "user_index": user_index, "conversation_id": conversation,
                "outcome": "failed", "confirmation_accepted": False, "confirmation_seconds": None,
                "job_id": None, "workflow_id": None, "admission": None, "error_code": None}
            await gate.wait()
            started = time.perf_counter()
            original_request = str(uuid4())
            try:
                async with asyncio.timeout(args.timeout):
                    async with client.stream("POST", f"{PREFIX}/{conversation}/messages/stream", headers=owner,
                        json={"request_id": original_request, "content": TEST_CONTENT}) as response:
                        response.raise_for_status()
                        turn_id = None
                        async for event, body in sse_events(response):
                            if not isinstance(body, dict):
                                raise ValueError("SSE event must contain an object")
                            if event == "accepted":
                                turn_id = str(UUID(body["turn_id"]))
                                if body["request_id"] != original_request:
                                    raise ValueError("Turn request identity mismatch")
                            elif event == "tool_finished":
                                trace = body.get("trace", {})
                                confirmation = trace.get("result", {}).get("confirmation")
                                if (confirmation and not sample["confirmation_accepted"] and turn_id
                                        and trace.get("status") == "completed" and trace.get("name") == "start_writing_intent"
                                        and confirmation.get("hook") == "start_writing_intent" and confirmation.get("state") == "pending"):
                                    confirmation_request = str(UUID(confirmation["input_request_id"]))
                                    secret = confirmation["token"]
                                    if not isinstance(secret, str) or re.fullmatch(r"[A-Za-z0-9_-]{20,128}", secret) is None:
                                        raise ValueError("Confirmation token shape is invalid")
                                    accepted = await client.post(f"{PREFIX}/{conversation}/turns/{turn_id}/inputs", headers=owner,
                                        json={"request_id": confirmation_request,
                                            "content": "capability-confirm:" + json.dumps({"token": secret, "action": "confirm"})})
                                    accepted.raise_for_status()
                                    sample.update(confirmation_accepted=True, confirmation_seconds=time.perf_counter() - started)
                            elif event == "done":
                                turn = body["turn"]
                                if turn["request_id"] != original_request or str(UUID(turn["id"])) != turn_id:
                                    raise ValueError("Persisted turn identity mismatch")
                                completed = next((trace for trace in reversed(turn.get("tools", []))
                                    if trace.get("name") == "start_writing_intent" and trace.get("status") == "completed"
                                    and trace.get("result", {}).get("job_id")), None)
                                if turn["status"] != "completed" or completed is None:
                                    raise ValueError("Writing start did not return a persisted successful trace")
                                result = completed["result"]
                                job_id = str(UUID(result["job_id"]))
                                if result["workflow_id"] != f"news-writing:{owner['X-Tenant-ID']}:{job_id}":
                                    raise ValueError("Writing Workflow reference is not bound to the scoped job")
                                sample.update(outcome="completed", job_id=job_id,
                                    workflow_id=result["workflow_id"], admission=admission_snapshot(result))
                            elif event == "error":
                                raise ValueError("SSE completed without an accepted writing result")
                        if sample["outcome"] != "completed":
                            raise ValueError("SSE ended without a persisted successful writing result")
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, AttributeError) as exc:
                sample["error_code"] = type(exc).__name__
            finally:
                sample["seconds"] = round(time.perf_counter() - started, 3)
                samples.append(sample)

        tasks = [asyncio.create_task(send(identity)) for identity in conversations]
        gate.set()
        await asyncio.gather(*tasks)
        until = time.perf_counter() + args.observe_seconds
        owners = {(tenant_index, user_index): owner for tenant_index, user_index, owner, _ in conversations}
        while time.perf_counter() < until:
            async def observe(sample):
                if not sample["job_id"]:
                    return
                item = {"tenant_index": sample["tenant_index"], "user_index": sample["user_index"],
                        "job_id": sample["job_id"], "seconds": round(time.perf_counter() - began, 3)}
                try:
                    response = await client.get(f"{PREFIX}/receipts/writing-jobs/{sample['job_id']}",
                        headers=owners[sample["tenant_index"], sample["user_index"]])
                    response.raise_for_status()
                    result = response.json()
                    if result["job"]["id"] != sample["job_id"]:
                        raise ValueError("Receipt entity mismatch")
                    item["admission"] = admission_snapshot(result)
                except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
                    item["error_code"] = type(exc).__name__
                observations.append(item)
            await asyncio.gather(*(observe(sample) for sample in samples))
            await asyncio.sleep(args.poll_interval)
        report = build_report(run_id=run_id, base_url=args.base_url, samples=samples,
            observations=observations, elapsed=time.perf_counter() - began, resources=resources)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"run_id": run_id, "confirmed_requests": report["confirmed_requests"],
            "verification_passed": report["verification_passed"],
            "failed_or_inconclusive_checks": report["failed_or_inconclusive_checks"]}, ensure_ascii=False))
        return 0 if report["verification_passed"] else 1


def main(argv=None):
    args = argument_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
        # Exception bodies can include untrusted response data or credentials.
        print(json.dumps({"verification_passed": False, "script_error": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
