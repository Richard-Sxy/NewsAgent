"""Synthetic HTTP only: exercise guarded load setup, SSE controls and reporting."""

import asyncio
from collections import Counter
from copy import deepcopy
from itertools import count
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from tools import conversation_hooks_load as load


def arguments(tmp_path, **changes):
    values = dict(base_url="http://127.0.0.1:28060", timeout=5, observe_seconds=3,
                  poll_interval=.1, token_env="NEWS_AGENT_HOOKS_LOAD_TOKEN",
                  resource_report=None, output=tmp_path / "report.json")
    values.update(changes)
    return SimpleNamespace(**values)


def snapshot(index, *, state="running", active_count=2, queue_position=None):
    return {"state": state, "active_count": active_count, "budget": 2,
            "queue_position": queue_position,
            "request_workflow_id": f"conversation-admission-request-{index:064x}",
            "admission_accepted": True, "workflow_accepted": state != "queued"}


def report_records():
    samples, observations = [], []
    for tenant in range(2):
        for user in range(3):
            admitted = snapshot(tenant * 3 + user, state="queued" if user == 2 else "running",
                                queue_position=1 if user == 2 else None)
            item = {"tenant_index": tenant, "user_index": user, "job_id": str(uuid4()),
                    "outcome": "completed", "confirmation_accepted": True,
                    "seconds": 1, "confirmation_seconds": .5, "admission": admitted}
            samples.append(item)
            observations.append({"tenant_index": tenant, "job_id": item["job_id"], "admission": admitted})
    return samples, observations


def report(samples, observations):
    return load.build_report(run_id="synthetic", base_url="http://127.0.0.1:28060",
                             samples=samples, observations=observations, elapsed=4)


@pytest.mark.parametrize("url", sorted(load.ALLOWED_URLS))
def test_cli_accepts_only_explicit_local_e2e_targets(monkeypatch, tmp_path, url):
    monkeypatch.setenv("ENVIRONMENT", "e2e")
    args = load.argument_parser().parse_args(["--base-url", url, "--output", str(tmp_path / "test.json")])
    load.validate_args(args)
    assert args.resource_report is None
    assert args.token_env == "NEWS_AGENT_HOOKS_LOAD_TOKEN"


@pytest.mark.parametrize("changes", [
    {"base_url": "https://enterprise.example"}, {"base_url": "http://127.0.0.1:28060/"},
    {"base_url": "http://127.0.0.1:28060@enterprise.example"}, {"timeout": 121},
    {"observe_seconds": 2}, {"poll_interval": 0}, {"token_env": "invalid-name"},
    {"output": "raw.log"}, {"resource_report": ".env.json"},
])
@pytest.mark.asyncio
async def test_invalid_arguments_reject_before_network(monkeypatch, tmp_path, changes):
    monkeypatch.setenv("ENVIRONMENT", "e2e")
    changes = {key: tmp_path / value if key in {"output", "resource_report"} else value
               for key, value in changes.items()}
    monkeypatch.setattr(load.httpx, "AsyncClient", lambda **kwargs: pytest.fail("network was accessed"))
    with pytest.raises(ValueError):
        await load.run(arguments(tmp_path, **changes))


@pytest.mark.asyncio
async def test_non_e2e_environment_and_environment_symlink_are_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(load.httpx, "AsyncClient", lambda **kwargs: pytest.fail("network was accessed"))
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="ENVIRONMENT"):
        await load.run(arguments(tmp_path))
    monkeypatch.setenv("ENVIRONMENT", "e2e")
    alias = tmp_path / "resource.json"
    try:
        alias.symlink_to(tmp_path / ".env")
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
    with pytest.raises(ValueError, match="environment files"):
        await load.run(arguments(tmp_path, resource_report=alias))


def test_resource_report_whitelists_aggregate_fields_and_bounds_size(tmp_path):
    source = tmp_path / "stats.json"
    source.write_text(json.dumps({"containers": [
        {"Name": "worker-writing", "CPUPercent": "124.5%", "MemPerc": "3.2%",
         "MemUsage": "150MiB / 8GiB", "PIDs": "12", "token": "do-not-copy",
         "message": "raw-user-content"},
        {"Name": "unsafe\nname", "CPUPercent": "10%"},
        {"name": "api", "CPUPerc": "12.5%", "MemPerc": "unknown", "PIDs": "secret"},
    ]}))
    value = load.resource_summary(source)
    assert value["containers"] == [
        {"name": "worker-writing", "cpu_percent": 124.5, "memory_percent": 3.2,
         "memory_usage": "150MiB / 8GiB", "pids": 12}, {"name": "api", "cpu_percent": 12.5}]
    assert "do-not-copy" not in json.dumps(value)
    assert load.resource_summary(None) is None
    source.write_bytes(b" " * 1048577)
    with pytest.raises(ValueError, match="bounded JSON size"):
        load.resource_summary(source)


def test_report_requires_observed_budget_queue_receipts_and_all_confirmations():
    samples, observations = report_records()
    value = report(samples, observations)
    assert value["verification_passed"] is True
    assert value["confirmed_requests"] == 6
    assert [item["peak_observed_active_count"] for item in value["tenants"]] == [2, 2]
    assert load.percentiles([1, 2, 3, 4]) == {"p50": 2, "p95": 4, "max": 4}
    assert load.percentiles([]) is None
    samples[0]["confirmation_accepted"] = False
    observations[1]["admission"] = None
    observations[2]["admission"]["active_count"] = 3
    value = report(samples, observations)
    assert value["verification_passed"] is False
    assert set(value["failed_or_inconclusive_checks"]) >= {
        "tenant_0:three_confirmed_requests", "tenant_0:budget_not_exceeded", "tenant_0:all_receipts_observed"}


def test_report_marks_absent_queue_evidence_and_increasing_position_inconclusive():
    samples, observations = report_records()
    for item in samples + observations:
        item["admission"] = {**item["admission"], "state": "terminal", "queue_position": None}
    value = report(samples, observations)
    assert value["verification_passed"] is False
    assert "tenant_0:queue_observed" in value["failed_or_inconclusive_checks"]
    samples, observations = report_records()
    later = deepcopy(observations[2])
    later["admission"]["queue_position"] = 2
    observations.append(later)
    value = report(samples, observations)
    assert "tenant_0:fifo_queue_positions_nonincreasing" in value["failed_or_inconclusive_checks"]


@pytest.mark.parametrize("change", [
    {"request_workflow_id": "fake-reference"}, {"active_count": True}, {"budget": 3},
    {"state": "made_up"}, {"queue_position": 0},
])
def test_admission_snapshot_requires_strict_authoritative_values(change):
    value = snapshot(1)
    value.update(change)
    with pytest.raises(ValueError):
        load.admission_snapshot({"admission": value, "admission_accepted": True, "workflow_accepted": True})


def test_queue_cannot_claim_business_acceptance_or_unaccepted_request():
    source = snapshot(1, state="queued", queue_position=1, active_count=None)
    with pytest.raises(ValueError, match="must not claim"):
        load.admission_snapshot({"admission": source, "admission_accepted": True, "workflow_accepted": True})
    with pytest.raises(ValueError, match="not been durably admitted"):
        load.admission_snapshot({"admission": source, "admission_accepted": False, "workflow_accepted": False})
    assert load.admission_snapshot({"admission": source, "admission_accepted": True,
                                    "workflow_accepted": False})["active_count"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [
    {"model_provider": "enterprise"}, {"available_tools": []},
    {"async_admission": {"enabled": False, "budget": 2, "scope": "conversation_async_hooks"}},
    {"async_admission": {"enabled": True, "budget": 8, "scope": "conversation_async_hooks"}},
])
async def test_runtime_preflight_blocks_before_any_writes(monkeypatch, tmp_path, runtime):
    monkeypatch.setenv("ENVIRONMENT", "e2e")
    seen = []
    baseline = {"model_provider": "local", "available_tools": ["start_writing_intent"],
                "async_admission": {"enabled": True, "budget": 2, "scope": "conversation_async_hooks"}}

    async def handle(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={**baseline, **runtime})

    original = httpx.AsyncClient
    monkeypatch.setattr(load.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)))
    with pytest.raises(ValueError, match="Runtime must be local"):
        await load.run(arguments(tmp_path))
    assert seen == [("GET", load.PREFIX + "/runtime")]
    assert not arguments(tmp_path).output.exists()


@pytest.mark.asyncio
async def test_six_concurrent_confirmations_remain_scoped_and_reports_drop_secrets(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ENVIRONMENT", "e2e")
    gateway_secret = "TEST_GATEWAY_SECRET_NEVER_REPORTED"
    monkeypatch.setenv("NEWS_AGENT_HOOKS_LOAD_TOKEN", gateway_secret)
    records, jobs, confirmations, requests = {}, {}, Counter(), []
    streaming = peak_streaming = 0

    def encode(event, body):
        return f"event: {event}\ndata: {json.dumps(body)}\n\n".encode()

    class Stream(httpx.AsyncByteStream):
        def __init__(self, record):
            self.record = record

        async def __aiter__(self):
            nonlocal streaming, peak_streaming
            record = self.record
            streaming += 1
            peak_streaming = max(peak_streaming, streaming)
            try:
                yield encode("accepted", {"turn_id": record["turn_id"], "request_id": record["request_id"]})
                yield b": keep-alive\n\n"
                yield encode("tool_finished", {"trace": {"name": "start_writing_intent", "status": "completed",
                    "result": {"confirmation": {"hook": "start_writing_intent", "state": "pending",
                        "input_request_id": record["input_request"], "token": record["token"]}}}})
                await record["confirmed"].wait()
                # Keep all six streams open until the whole burst has reached confirmation.
                await all_confirmed.wait()
                yield encode("done", {"turn": {"id": record["turn_id"], "request_id": record["request_id"],
                    "status": "completed", "assistant_content": "private-model-expression", "tools": [
                        {"name": "start_writing_intent", "status": "completed", "result": record["result"]}]}})
            finally:
                streaming -= 1

    all_confirmed = asyncio.Event()

    async def handle(request):
        requests.append((request.method, request.url.path))
        owner = (request.headers["X-Tenant-ID"], request.headers["X-User-ID"])
        UUID(owner[0]); UUID(owner[1])
        assert request.headers["Authorization"] == f"Bearer {gateway_secret}"
        assert request.headers["X-Writing-Roles"] == "writing:admin"
        path = request.url.path
        if path.endswith("/runtime"):
            return httpx.Response(200, json={"model_provider": "local", "available_tools": ["start_writing_intent"],
                "async_admission": {"enabled": True, "budget": 2, "queue_limit": 100, "scope": "conversation_async_hooks"}})
        if path == load.PREFIX:
            conversation = str(uuid4())
            records[conversation] = {"owner": owner}
            return httpx.Response(201, json={"id": conversation})
        if "/receipts/writing-jobs/" in path:
            job_id = path.rsplit("/", 1)[1]
            record = jobs[job_id]
            assert record["owner"] == owner
            return httpx.Response(200, json={**record["result"], "job": {"id": job_id}})
        conversation = path.split("/")[4]
        record = records[conversation]
        assert record["owner"] == owner
        body = json.loads(request.content)
        if path.endswith("/messages/stream"):
            assert body["content"] == load.TEST_CONTENT
            record.update(turn_id=str(uuid4()), request_id=body["request_id"], input_request=str(uuid4()),
                          token="transient_confirmation_token_" + uuid4().hex, confirmed=asyncio.Event())
            return httpx.Response(200, stream=Stream(record), headers={"Content-Type": "text/event-stream"})
        assert path == f"{load.PREFIX}/{conversation}/turns/{record['turn_id']}/inputs"
        assert body["request_id"] == record["input_request"]
        assert json.loads(body["content"].removeprefix("capability-confirm:")) == {
            "action": "confirm", "token": record["token"]}
        confirmations[owner[0]] += 1
        queued = confirmations[owner[0]] == 3
        job_id = str(uuid4())
        admitted = snapshot(len(jobs), state="queued" if queued else "running", queue_position=1 if queued else None)
        record["result"] = {"job_id": job_id, "workflow_id": f"news-writing:{owner[0]}:{job_id}",
                            "admission": admitted, "admission_accepted": True, "workflow_accepted": not queued,
                            "raw_token": gateway_secret, "original_content": load.TEST_CONTENT}
        jobs[job_id] = record
        record["confirmed"].set()
        if sum(confirmations.values()) == 6:
            all_confirmed.set()
        return httpx.Response(202, json={"id": str(uuid4())})

    original = httpx.AsyncClient
    monkeypatch.setattr(load.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)))
    ticks = count()
    monkeypatch.setattr(load, "time", SimpleNamespace(perf_counter=lambda: next(ticks) * .02))
    original_sleep = asyncio.sleep

    async def instant_sleep(seconds):
        await original_sleep(0)

    monkeypatch.setattr(load.asyncio, "sleep", instant_sleep)
    args = arguments(tmp_path)
    assert await load.run(args) == 0
    serialized = args.output.read_text()
    value = json.loads(serialized)
    assert value["verification_passed"] is True
    assert value["confirmed_requests"] == 6
    assert peak_streaming == 6
    assert sorted(confirmations.values()) == [3, 3]
    assert len({record["owner"][1] for record in records.values()}) == 6
    assert all(item["verification"]["queue_observed"] for item in value["tenants"])
    exposed = serialized + capsys.readouterr().out
    assert gateway_secret not in exposed
    assert load.TEST_CONTENT not in exposed
    assert "private-model-expression" not in exposed
    assert all(record["token"] not in exposed for record in records.values())
    assert all(method != "DELETE" and not any(word in path for word in ("approve", "publish", "cancel"))
               for method, path in requests)
    assert len([path for method, path in requests if method == "POST" and path.endswith("/inputs")]) == 6


def test_cli_failure_prints_only_exception_class(monkeypatch, tmp_path, capsys):
    async def blocked(args):
        raise ValueError("secret-http-response-body")

    monkeypatch.setattr(load, "run", blocked)
    assert load.main(["--output", str(tmp_path / "report.json")]) == 2
    assert capsys.readouterr().err == '{"verification_passed": false, "script_error": "ValueError"}\n'
