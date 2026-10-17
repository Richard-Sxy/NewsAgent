"""Writing confirmations expose committed snapshots and preserve jobs on disconnect."""

import asyncio
import json

import pytest
from sqlalchemy.exc import OperationalError

from app.conversation.streaming import StreamingConversation
from app.repositories.conversation import PostgresConversationRepository
from tests.test_conversation_writing_confirm import (
    ADMIN, QUESTION, persisted_proposal, prepare_writing, submit_confirmation, writing_harness,
)


def decode(frame):
    if frame.startswith(":"):
        return "heartbeat", {}
    fields = frame.splitlines()
    return fields[0].removeprefix("event: "), json.loads(fields[1].removeprefix("data: "))


def writing_stream(service, claim, identity):
    return StreamingConversation(service=service, claim=claim, **identity,
        permissions=ADMIN, heartbeat_seconds=0.02, chunk_chars=128).iter_events()


async def next_finished(iterator, *, index=0):
    async with asyncio.timeout(2):
        async for frame in iterator:
            name, data = decode(frame)
            if name == "tool_finished" and data["index"] == index:
                return data
            assert name not in {"done", "error"}, data
    raise AssertionError("Expected the persisted writing tool snapshot")


@pytest.mark.asyncio
async def test_stream_pending_confirmation_is_durable_then_accepted_result_finishes_immediately():
    service, repository, _, jobs, orchestrator, inference = writing_harness()
    claim, identity = await prepare_writing(service, repository)
    iterator = writing_stream(service, claim, identity)
    first = await next_finished(iterator)
    persisted, confirmation = await persisted_proposal(repository, identity)
    assert first["trace"] == persisted.tools[-1].model_dump(mode="json")
    assert not jobs.calls and not orchestrator.calls
    await submit_confirmation(service, claim, identity, confirmation)
    accepted = await next_finished(iterator, index=1)
    persisted = (await repository.get(**identity)).turns[-1]
    assert accepted["trace"] == persisted.tools[-1].model_dump(mode="json")
    assert accepted["trace"]["result"]["workflow_accepted"] is True
    frames = [decode(frame) async for frame in iterator]
    done = next(data["turn"] for name, data in frames if name == "done")
    assert done["status"] == "completed" and len(jobs.calls) == len(orchestrator.calls) == 1
    assert len(inference.calls) == 1, "The accepted job receipt must not wait for another model explanation"
    assert any(name == "answer_delta" for name, _ in frames)


@pytest.mark.asyncio
async def test_disconnect_before_confirmation_keeps_proposal_and_starts_nothing():
    service, repository, _, jobs, orchestrator, _ = writing_harness()
    claim, identity = await prepare_writing(service, repository)
    iterator = writing_stream(service, claim, identity)
    await next_finished(iterator)
    await iterator.aclose()
    turn = (await repository.get(**identity)).turns[-1]
    assert turn.status == "failed" and turn.error_code == "interrupted"
    assert len(turn.tools) == 1 and turn.tools[0].result["confirmation"]["state"] == "pending"
    assert not jobs.calls and not orchestrator.calls


@pytest.mark.asyncio
async def test_disconnect_during_temporal_acceptance_retains_committed_job_binding():
    service, repository, _, jobs, orchestrator, _ = writing_harness()
    orchestrator.release = asyncio.Event()
    claim, identity = await prepare_writing(service, repository)
    iterator = writing_stream(service, claim, identity)
    await next_finished(iterator)
    _, confirmation = await persisted_proposal(repository, identity)
    await submit_confirmation(service, claim, identity, confirmation)
    await asyncio.wait_for(orchestrator.started.wait(), timeout=2)
    await iterator.aclose()
    turn = (await repository.get(**identity)).turns[-1]
    assert turn.error_code == "interrupted" and len(jobs.rows) == 1
    bound = turn.tools[-1].result
    assert bound["job_id"] == str(orchestrator.calls[0].id)
    assert bound["workflow_id"] == orchestrator.calls[0].temporal_workflow_id
    assert bound["receipt"]["entity_id"] == bound["job_id"]
    assert bound["workflow_accepted"] is False
    assert len(orchestrator.calls) == 1
    replay = await service.send(**identity, request_id=claim.turn.request_id, content=QUESTION, permissions=ADMIN)
    assert replay.id == turn.id and len(orchestrator.calls) == 1


@pytest.mark.asyncio
async def test_disconnect_after_acceptance_preserves_workflow_and_does_not_rewrite_pending_trace():
    finishing = asyncio.Event()
    class PausedFinishRepository(PostgresConversationRepository):
        async def finish_or_claim(self, **kwargs):
            finishing.set()
            await asyncio.Event().wait()
    service, repository, _, jobs, orchestrator, _ = writing_harness(repository_type=PausedFinishRepository)
    claim, identity = await prepare_writing(service, repository)
    iterator = writing_stream(service, claim, identity)
    proposal = await next_finished(iterator)
    _, confirmation = await persisted_proposal(repository, identity)
    await submit_confirmation(service, claim, identity, confirmation)
    accepted = await next_finished(iterator, index=1)
    await asyncio.wait_for(finishing.wait(), timeout=2)
    await iterator.aclose()
    turn = (await repository.get(**identity)).turns[-1]
    assert turn.error_code == "interrupted" and len(jobs.rows) == len(orchestrator.calls) == 1
    assert turn.tools[0].model_dump(mode="json") == proposal["trace"]
    assert turn.tools[1].model_dump(mode="json") == accepted["trace"]
    assert turn.tools[1].result["workflow_accepted"] is True
    assert turn.tools[1].result["receipt"]["entity_id"] == str(orchestrator.calls[0].id)


@pytest.mark.asyncio
async def test_failed_proposal_checkpoint_cannot_emit_confirmation_or_start_job():
    class FailedCheckpointRepository(PostgresConversationRepository):
        async def checkpoint_turn(self, **kwargs):
            raise OperationalError("checkpoint unavailable", None, Exception("temporary outage"))
    service, repository, _, jobs, orchestrator, _ = writing_harness(repository_type=FailedCheckpointRepository)
    claim, identity = await prepare_writing(service, repository)
    events = [decode(frame) async for frame in writing_stream(service, claim, identity)]
    assert not [data for name, data in events if name == "tool_finished"]
    assert not jobs.calls and not orchestrator.calls
    assert any(name in {"done", "error"} for name, _ in events)

