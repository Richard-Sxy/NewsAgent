"""Local HTTP/SSE load test; synthetic identities, real persisted Agent turns.

No .env loading, existing-history deletion, workflow creation or publication.
The local API may call a real billed model: inspect runtime before a large run.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from uuid import uuid4

import httpx

PREFIX = "/api/v1/conversations"
LOCAL_TOKEN = "newsagent-native-local-token-not-for-production"
TENANT = "11111111-1111-4111-8111-111111111111"


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return None
    return {name: round(ordered[max(0, math.ceil(len(ordered) * q) - 1)], 3)
            for name, q in (("p50", .5), ("p95", .95), ("p99", .99), ("max", 1))}


async def run(args):
    if args.base_url not in {"http://127.0.0.1:28000", "http://127.0.0.1:28030", "http://api:8000"}:
        raise ValueError("Only isolated demo ports 28000/28030 or Compose api:8000 are accepted")
    run_id = uuid4().hex
    headers = {"Authorization": f"Bearer {LOCAL_TOKEN}", "X-Tenant-ID": TENANT,
               "X-Hot-News-Roles": "hot-news:read"}
    limits = httpx.Limits(max_connections=args.users + 10, max_keepalive_connections=args.users)
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, limits=limits,
                                 timeout=args.timeout, trust_env=False, follow_redirects=False) as client:
        runtime_response = await client.get(f"{PREFIX}/runtime", headers={"X-User-ID": str(uuid4())})
        runtime_response.raise_for_status()
        runtime = runtime_response.json()
        print(json.dumps({"stage": "runtime", "model_provider": runtime.get("model_provider"),
                          "model_route": runtime.get("model_route"), "users": args.users}, ensure_ascii=False), flush=True)
        identities = []
        # Setup is outside the measured burst, avoiding a create->send ramp.
        for index in range(args.users):
            user = {"X-User-ID": str(uuid4())}
            response = await client.post(PREFIX, headers=user,
                                         json={"title": f"load-{run_id[:8]}-{index}"})
            response.raise_for_status()
            identities.append((user, response.json()["id"]))
        samples, turns = [], {}
        active = peak = 0

        async def send(index, round_index, gate):
            nonlocal active, peak
            user, conversation = identities[index]
            content = ("你好" if args.workload == "greeting" or index % 2 == 0 else
                       "读取已完成的热点报告，使用 list_hot_news 和 read_hot_news，不创建新查询。")
            request_id = str(uuid4())
            payload = {"request_id": request_id, "content": content}
            await gate.wait()
            started = time.perf_counter()
            active += 1
            peak = max(peak, active)
            sample = {"user_index": index, "round": round_index, "scenario": content,
                      "http_status": None, "status": "transport_failed", "error_code": None,
                      "first_event_seconds": None, "first_answer_seconds": None}
            turn = None
            try:
                async with asyncio.timeout(args.timeout):
                    if args.transport == "json":
                        response = await client.post(f"{PREFIX}/{conversation}/messages", headers=user, json=payload)
                        sample["http_status"] = response.status_code
                        if response.status_code == 200:
                            turn = response.json()
                    else:
                        async with client.stream("POST", f"{PREFIX}/{conversation}/messages/stream",
                                                 headers=user, json=payload) as response:
                            sample["http_status"] = response.status_code
                            if response.status_code == 200:
                                event, data = "message", []
                                async for line in response.aiter_lines():
                                    if line.startswith("event:"):
                                        event = line[6:].strip()
                                    elif line.startswith("data:"):
                                        data.append(line[5:].lstrip())
                                    elif not line and data:
                                        elapsed = time.perf_counter() - started
                                        body = json.loads("\n".join(data))
                                        if sample["first_event_seconds"] is None:
                                            sample["first_event_seconds"] = elapsed
                                        if event == "answer_delta" and sample["first_answer_seconds"] is None:
                                            sample["first_answer_seconds"] = elapsed
                                        if event == "done":
                                            turn = body["turn"]
                                        elif event == "error":
                                            sample["error_code"] = body.get("code", "stream_error")
                                        event, data = "message", []
                    if turn is not None:
                        if turn.get("request_id") != request_id or turn.get("user_content") != content:
                            raise ValueError("turn_identity_mismatch")
                        sample.update(status=turn["status"], error_code=turn.get("error_code"),
                                      model_calls=len(turn.get("model_request_ids", [])),
                                      tool_errors=[t.get("error_code") or t["status"] for t in turn.get("tools", [])
                                                   if t["status"] != "completed"])
                        turns[index, round_index] = (payload, turn)
                    else:
                        sample["status"] = "http_failed" if sample["http_status"] != 200 else "missing_terminal"
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError) as exc:
                # Report classes only; response bodies may contain provider data.
                sample["error_code"] = type(exc).__name__
            finally:
                sample["seconds"] = time.perf_counter() - started
                active -= 1
                samples.append(sample)

        began = time.perf_counter()
        for round_index in range(args.rounds):
            gate = asyncio.Event()
            tasks = [asyncio.create_task(send(i, round_index, gate)) for i in range(args.users)]
            gate.set()
            await asyncio.gather(*tasks)
            print(json.dumps({"stage": "round_done", "round": round_index + 1,
                              "statuses": dict(Counter(s["status"] for s in samples))}), flush=True)
        elapsed = time.perf_counter() - began
        # Bounded verification concurrency; excluded from load timings.
        slots = asyncio.Semaphore(10)

        async def verify(index):
            async with slots:
                user, conversation = identities[index]
                checks = {"history": False, "replay": False, "other_user_denied": False, "other_tenant_denied": False}
                try:
                    history = await client.get(f"{PREFIX}/{conversation}", headers=user)
                    saved = history.json()["turns"] if history.status_code == 200 else []
                    expected = [turns[index, r][1] for r in range(args.rounds) if (index, r) in turns]
                    checks["history"] = len(expected) == args.rounds and saved == expected
                    if expected:
                        payload, original = turns[index, max(r for i, r in turns if i == index)]
                        replay = await client.post(f"{PREFIX}/{conversation}/messages", headers=user, json=payload)
                        checks["replay"] = replay.status_code == 200 and replay.json() == original
                    outsider = identities[(index + 1) % args.users][0] if args.users > 1 else {"X-User-ID": str(uuid4())}
                    denied = await client.get(f"{PREFIX}/{conversation}", headers=outsider)
                    checks["other_user_denied"] = denied.status_code == 404
                    denied = await client.get(f"{PREFIX}/{conversation}", headers={**user, "X-Tenant-ID": str(uuid4())})
                    checks["other_tenant_denied"] = denied.status_code == 404
                except (httpx.HTTPError, ValueError, KeyError):
                    pass
                return checks

        checks = await asyncio.gather(*(verify(i) for i in range(args.users)))
        completed = sum(s["status"] == "completed" for s in samples)
        clean_completed = sum(s["status"] == "completed" and not s.get("tool_errors") for s in samples)
        report = {"run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
                  "base_url": args.base_url, "runtime": runtime, "users": args.users,
                  "rounds": args.rounds, "transport": args.transport, "workload": args.workload,
                  "client_peak_inflight": peak, "burst_seconds": round(elapsed, 3),
                  "attempted_turns": len(samples), "completed_turns": completed,
                  "clean_completed_turns": clean_completed,
                  "tool_errors": dict(Counter(e for s in samples for e in s.get("tool_errors", []))),
                  "completed_turns_per_second": round(completed / elapsed, 3),
                  "statuses": dict(Counter(s["status"] for s in samples)),
                  "http_statuses": dict(Counter(str(s["http_status"]) for s in samples)),
                  "errors": dict(Counter(s["error_code"] for s in samples if s["error_code"])),
                  "latency_seconds_all": distribution([s["seconds"] for s in samples]),
                  "latency_seconds_completed": distribution([s["seconds"] for s in samples if s["status"] == "completed"]),
                  "first_event_seconds": distribution([s["first_event_seconds"] for s in samples if s["first_event_seconds"] is not None]),
                  "first_answer_seconds": distribution([s["first_answer_seconds"] for s in samples if s["first_answer_seconds"] is not None]),
                  "scenarios": {name: {"statuses": dict(Counter(s["status"] for s in samples if s["scenario"] == name)),
                                       "latency_seconds": distribution([s["seconds"] for s in samples if s["scenario"] == name])}
                                for name in sorted({s["scenario"] for s in samples})},
                  "verification_passes": {key: sum(c[key] for c in checks) for key in checks[0]},
                  "samples": samples,
                  "limitations": ["client inflight is not server/model concurrency", "burst test is not sustained capacity",
                                   "no new hot-news workflow, writing, knowledge search or context compaction load",
                                   "synthetic users share one tenant; different-tenant access denial is checked",
                                   "SSE answer is emitted after validation/persistence, not raw model TTFT"]}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key not in {"runtime", "samples", "limitations"}}, ensure_ascii=False), flush=True)
        return 0 if clean_completed == len(samples) and all(all(c.values()) for c in checks) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:28000")
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--transport", choices=("json", "sse"), default="sse")
    parser.add_argument("--workload", choices=("greeting", "mixed"), default="mixed")
    parser.add_argument("--timeout", type=float, default=150)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (1 <= args.users <= 100 and 1 <= args.rounds <= 20 and 5 <= args.timeout <= 300):
        parser.error("users 1..100, rounds 1..20, timeout 5..300 required")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
