"""Admission safety and optional real Redis, using a unique synthetic pool per test."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError

from app.model_runtime.governance import GovernancePolicy, GovernedInference, RedisModelAdmission, govern_inference
from app.model_runtime.http import ModelTransportError
from app.model_runtime.config_file import load_model_runtime_config
import yaml


def policy(**kw):
    return GovernancePolicy(version="test-v1", pool="test-"+uuid4().hex,
        max_concurrent=3, interactive_reserved=1, requests_per_minute=10,
        interactive_rpm_reserved=2, **kw)


def test_config_reserves_positive_background_capacity():
    with pytest.raises(ValueError):
        GovernancePolicy(**{**policy().model_dump(), "interactive_reserved": 3})


def test_disabled_does_not_open_redis(monkeypatch):
    monkeypatch.delenv("MODEL_GOVERNANCE_CONFIG_PATH", raising=False)
    inner = object()
    assert govern_inference(inner) is inner


def test_local_policy_names_match_registered_scenes():
    deploy = Path(__file__).resolve().parents[1]/"deploy"
    configured = GovernancePolicy.model_validate(yaml.safe_load((deploy/"model-governance.local.yml").read_text()))
    runtime = load_model_runtime_config(deploy/"model-runtime.local.yml")
    assert "text2sql_assistant" in configured.interactive_scenes
    assert set(configured.interactive_scenes) <= {s.scene for s in runtime.agent_scenes}


@pytest.mark.asyncio
async def test_backend_failure_never_calls_model():
    class Broken:
        async def eval(self, *args):
            raise ConnectionError("PRIVATE")
    class Model:
        async def complete(self, request):
            pytest.fail("must fail closed")
    wrapped = GovernedInference(Model(), RedisModelAdmission(Broken(), policy()))
    with pytest.raises(ModelTransportError) as error:
        await wrapped.complete(SimpleNamespace(scene="conversation", timeout_seconds=1))
    assert error.value.failure_kind == "quota_backend_unavailable"
    assert "PRIVATE" not in str(error.value)


@pytest.mark.asyncio
async def test_cancel_and_deadline_release_admission():
    class Admission:
        policy = policy()
        released = []
        async def acquire(self, *args, **kwargs):
            return "lease"
        async def release(self, ticket):
            self.released.append(ticket)
    class Slow:
        async def complete(self, request):
            await asyncio.sleep(10)
    admission = Admission()
    wrapped = GovernedInference(Slow(), admission)
    with pytest.raises(ModelTransportError) as error:
        await wrapped.complete(SimpleNamespace(scene="conversation", timeout_seconds=.02))
    assert error.value.failure_kind == "model_deadline"
    assert error.value.failure_stage == "inference"
    assert admission.released == ["lease"]
    task = asyncio.create_task(wrapped.complete(SimpleNamespace(scene="conversation", timeout_seconds=5)))
    await asyncio.sleep(.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.released == ["lease", "lease"]


@pytest.fixture
def redis_url():
    url = os.environ.get("MODEL_GOVERNANCE_TEST_REDIS_URL")
    if not url:
        pytest.skip("explicit isolated Redis required")
    return url


@pytest.mark.asyncio
async def test_real_redis_replica_capacity_reservation_rpm_and_policy(redis_url):
    p = policy(wait_seconds=.08)
    clients = [Redis.from_url(redis_url) for _ in range(3)]
    replicas = [RedisModelAdmission(c, p) for c in clients]
    try:
        a, b = await asyncio.gather(*(r.acquire("background", call_seconds=1) for r in replicas[:2]))
        with pytest.raises(ModelTransportError, match="admission"):
            await replicas[2].acquire("background", call_seconds=1)
        c = await replicas[2].acquire("interactive", call_seconds=1)
        assert await clients[0].zcard(replicas[0].keys[1]) == 3
        with pytest.raises(ModelTransportError):
            await replicas[0].acquire("interactive", call_seconds=1)
        await asyncio.gather(*(r.release(t) for r, t in zip(replicas, (a,b,c))))
        assert await clients[0].zcard(replicas[0].keys[1]) == 0
        assert await clients[0].zcard(replicas[0].keys[3]) == 3
        changed = RedisModelAdmission(clients[0], p.model_copy(update={"max_concurrent": 4}))
        with pytest.raises(ModelTransportError) as error:
            await changed.acquire("interactive", call_seconds=1)
        assert error.value.failure_kind == "quota_policy_mismatch"
        # Released/failed calls still consume the rolling minute allowance.
        for _ in range(6):
            ticket = await replicas[0].acquire("background", call_seconds=1)
            await replicas[0].release(ticket)
        with pytest.raises(ModelTransportError):
            await replicas[0].acquire("background", call_seconds=1)
        ticket = await replicas[0].acquire("interactive", call_seconds=1)
        await replicas[0].release(ticket)
        with pytest.raises(ModelTransportError):
            await replicas[0].acquire("interactive", call_seconds=1)
    finally:
        await clients[0].delete(*replicas[0].keys)
        for client in clients:
            await client.aclose()


@pytest.mark.asyncio
async def test_expired_lease_and_old_rpm_recover_without_releasing_other_owner(redis_url):
    client = Redis.from_url(redis_url)
    r = RedisModelAdmission(client, policy())
    try:
        await client.zadd(r.keys[1], {"crashed": 1})
        await client.zadd(r.keys[2], {"crashed": 1})
        await client.zadd(r.keys[3], {"old-minute": 1})
        ticket = await r.acquire("interactive", call_seconds=1)
        await r.release("another-owner")
        assert await client.zcard(r.keys[1]) == 1
        assert await client.zcard(r.keys[3]) == 1
        await r.release(ticket)
    finally:
        await client.delete(*r.keys)
        await client.aclose()


@pytest.mark.asyncio
async def test_real_redis_six_processes_share_one_capacity(redis_url):
    p = policy(wait_seconds=5)
    client = Redis.from_url(redis_url)
    admission = RedisModelAdmission(client,p)
    gate = admission.keys[0]+":test-start"
    ready = gate+":ready"
    script = '''
import asyncio,json,os,sys
from redis.asyncio import Redis
from app.model_runtime.governance import GovernancePolicy,RedisModelAdmission
async def main():
    r=Redis.from_url(os.environ['MODEL_GOVERNANCE_TEST_REDIS_URL'])
    a=RedisModelAdmission(r,GovernancePolicy.model_validate_json(sys.argv[1]))
    await r.incr(sys.argv[2]+':ready')
    while not await r.get(sys.argv[2]): await asyncio.sleep(.02)
    ticket=await a.acquire(sys.argv[3],call_seconds=2)
    result={'active':await r.zcard(a.keys[1]),'background':await r.zcard(a.keys[2])}
    await asyncio.sleep(.3)
    await a.release(ticket)
    await r.aclose()
    print(json.dumps(result))
asyncio.run(main())
'''
    processes = []
    try:
        for lane in ("background",)*3+("interactive",)*3:
            processes.append(await asyncio.create_subprocess_exec(sys.executable,"-c",script,p.model_dump_json(),gate,lane,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE))
        async with asyncio.timeout(20):
            while int(await client.get(ready) or 0) < 6:
                await asyncio.sleep(.05)
            await client.set(gate,"1",ex=30)
            results = await asyncio.gather(*(process.communicate() for process in processes))
        assert all(process.returncode == 0 for process in processes), results
        stats = [json.loads(stdout) for stdout,_ in results]
        assert max(s["active"] for s in stats) == 3
        assert max(s["background"] for s in stats) <= 2
        assert await client.zcard(admission.keys[1]) == 0
        assert await client.zcard(admission.keys[3]) == 6
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        await client.delete(*admission.keys,gate,ready)
        await client.aclose()
